#!/usr/bin/env python3
"""Exercise the running Compose stack. --faults stops/restarts local services.

Creates test users/dialogs; never logs credentials. Run only against a development stack.
"""

import argparse
import asyncio
from contextlib import AsyncExitStack
import json
from pathlib import Path
import time
from uuid import uuid4

import httpx
from websockets.asyncio.client import connect

ROOT = Path(__file__).resolve().parents[1]


async def compose(*args):
    process = await asyncio.create_subprocess_exec(
        "docker",
        "compose",
        *args,
        cwd=ROOT,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    out, err = await process.communicate()
    if process.returncode:
        raise RuntimeError(f"Compose {args[0]} failed: {err.decode()[-1000:]}")
    return out.decode()


async def frame(ws, expected, timeout=30):
    async with asyncio.timeout(timeout):
        while True:
            data = json.loads(await ws.recv())
            if data["type"] == "ping":
                await ws.send(json.dumps({"type": "pong"}))
            elif data["type"] == expected:
                return data


async def opened(stack, url, origin, token):
    ws = await stack.enter_async_context(connect(url, origin=origin, proxy=None))
    await ws.send(json.dumps({"type": "auth", "access_token": token}))
    await frame(ws, "ready")
    return ws


async def eventually(check, seconds=60):
    deadline = time.monotonic() + seconds
    while True:
        try:
            result = await check()
            if result:
                return result
        except (httpx.HTTPError, OSError):
            pass
        if time.monotonic() >= deadline:
            raise AssertionError("Timed out waiting for service recovery")
        await asyncio.sleep(0.5)


async def run(args, execute=compose):
    started = time.time()
    results = []
    origin = args.base_url.rstrip("/")
    url = origin.replace("http://", "ws://").replace("https://", "wss://") + "/ws"
    async with AsyncExitStack() as stack:
        clients = [
            await stack.enter_async_context(
                httpx.AsyncClient(
                    base_url=origin,
                    timeout=12,
                    headers={"Origin": origin, "X-CSRF-Protection": "1"},
                )
            )
            for _ in range(3)
        ]

        async def public_ready():
            return (await clients[0].get("/")).status_code == 200 and (
                await clients[0].get("/auth/me")
            ).status_code == 401

        await eventually(public_ready)
        users, tokens = [], []
        password = "test-only-" + uuid4().hex
        for index, c in enumerate(clients):
            credentials = {
                "username": f"check{index}_{uuid4().hex[:12]}",
                "password": password,
            }
            r = await c.post("/auth/register", json=credentials)
            assert r.status_code == 201, r.text
            users.append(r.json())
            r = await c.post("/auth/login", json=credentials)
            assert r.status_code == 200, r.text
            assert (
                "HttpOnly" in r.headers["set-cookie"]
                and "SameSite=strict" in r.headers["set-cookie"]
            )
            tokens.append(r.json()["access_token"])
            c.headers["Authorization"] = "Bearer " + tokens[-1]
        a, b, c = clients
        pair = {"peer_user_id": users[1]["user_id"]}
        first, second = await asyncio.gather(
            a.post("/dialogs", json=pair), a.post("/dialogs", json=pair)
        )
        assert sorted([first.status_code, second.status_code]) == [200, 201]
        dialog = first.json()["dialog_id"]
        assert second.json()["dialog_id"] == dialog
        assert (
            await b.post("/dialogs", json={"peer_user_id": users[0]["user_id"]})
        ).json()["dialog_id"] == dialog
        assert (
            await a.post("/dialogs", json={"peer_user_id": users[0]["user_id"]})
        ).status_code == 422
        path = f"/dialogs/{dialog}/messages"
        wa, wb, wc = [await opened(stack, url, origin, token) for token in tokens]
        payload = {"text": "step4 trace", "client_message_id": str(uuid4())}
        sent, repeated = await asyncio.gather(
            a.post(path, json=payload), a.post(path, json=payload)
        )
        assert sorted([sent.status_code, repeated.status_code]) == [200, 201]
        message = sent.json()
        assert repeated.json() == message
        for ws in (wa, wb):
            event = await frame(ws, "message.created")
            assert event["message"] == message
        try:
            await frame(wc, "message.created", timeout=0.3)
            raise AssertionError("A non-participant received a message")
        except TimeoutError:
            pass
        # Warm cache, then prove all three operations still authorize C.
        assert (await a.get(path)).status_code == 200
        assert (await c.get("/dialogs/" + dialog)).status_code == 404
        assert (await c.get(path)).status_code == 404
        assert (
            await c.post(
                path, json={"text": "denied", "client_message_id": str(uuid4())}
            )
        ).status_code == 404
        assert (
            await a.post(path, json={**payload, "text": "different"})
        ).status_code == 409
        assert (
            await a.post("/internal/users/resolve", json={"user_ids": []})
        ).status_code == 404
        results += [
            "registration/login",
            "canonical-dialog-race",
            "idempotency",
            "HTTP-to-WS",
            "third-user-isolation",
            "internal-route-blocked",
        ]
        # Link the HTTP result to committed rows, event payload, confirmed outbox and logger.
        mid = message["message_id"]

        async def trace():
            sql = (
                "SELECT count(*) FROM messages m JOIN outbox_events e USING(message_id) "
                f"WHERE m.message_id='{mid}' AND e.published_at IS NOT NULL "
                f"AND e.payload->'message'->>'message_id'='{mid}';"
            )
            out = await execute(
                "exec",
                "-T",
                "postgres",
                "psql",
                "-U",
                "postgres",
                "-d",
                "chat_db",
                "-Atc",
                sql,
            )
            return out.strip() == "1"

        await eventually(trace)

        async def logged():
            return mid in await execute(
                "logs", "--no-color", "--tail", "1000", "consumer"
            )

        await eventually(logged)
        results.append("HTTP-database-outbox-logger-WS-trace")
        if args.faults:
            await wa.close()
            await wb.close()
            await wc.close()
            try:
                await execute("stop", "delivery")
                ids = []
                for _ in range(250):
                    r = await a.post(
                        path,
                        json={"text": "offline", "client_message_id": str(uuid4())},
                    )
                    assert r.status_code == 201, r.text
                    ids.append(r.json()["message_id"])
                await execute("up", "-d", "--wait", "--wait-timeout", "120", "delivery")
                wb = await opened(stack, url, origin, tokens[1])
                recovered, cursor = [], 1
                while cursor < 251:
                    r = await b.get(path, params={"after_seq": cursor, "limit": 100})
                    r.raise_for_status()
                    items = r.json()["items"]
                    assert items and items[0]["seq"] == cursor + 1
                    recovered.extend(items)
                    cursor = items[-1]["seq"]
                assert [m["seq"] for m in recovered] == list(range(2, 252))
                assert {m["message_id"] for m in recovered} == set(ids)
                results.append("delivery-restart-250-message-catchup")
                await wb.close()
                # Redis loss must not delay RabbitMQ publication.
                await execute("stop", "redis")
                wb = await opened(stack, url, origin, tokens[1])
                r = await a.post(
                    path, json={"text": "no redis", "client_message_id": str(uuid4())}
                )
                assert r.status_code == 201
                target = r.json()["message_id"]
                async with asyncio.timeout(30):
                    while (await frame(wb, "message.created"))["message"][
                        "message_id"
                    ] != target:
                        pass
                assert (await b.get(path)).status_code == 200
                results.append("redis-fallback-and-live-delivery")
                await execute("up", "-d", "--wait", "redis")
                await execute("stop", "rabbitmq")
                await asyncio.wait_for(wb.wait_closed(), 40)
                r = await a.post(
                    path,
                    json={"text": "broker down", "client_message_id": str(uuid4())},
                )
                assert r.status_code == 201
                saved = r.json()
                await execute("up", "-d", "--wait", "--wait-timeout", "150", "rabbitmq")
                await execute("up", "-d", "--wait", "--wait-timeout", "120", "delivery")
                wb = await opened(stack, url, origin, tokens[1])
                history = (
                    await b.get(path, params={"after_seq": saved["seq"] - 1})
                ).json()["items"]
                assert saved["message_id"] in {m["message_id"] for m in history}
                r = await a.post(
                    path,
                    json={"text": "broker restored", "client_message_id": str(uuid4())},
                )
                target = r.json()["message_id"]
                async with asyncio.timeout(30):
                    while (await frame(wb, "message.created"))["message"][
                        "message_id"
                    ] != target:
                        pass
                results.append("broker-loss-closes-WS-and-recovers")
                await execute("stop", "auth")
                assert (await a.get("/dialogs")).status_code == 200
                assert (await a.get(path)).status_code == 200
                assert (await a.post("/dialogs", json=pair)).status_code == 200
                assert (
                    await a.post("/dialogs", json={"peer_user_id": str(uuid4())})
                ).status_code == 503
                await execute("up", "-d", "--wait", "auth")
                results.append("existing-dialogs-survive-auth-outage")
                for service in ("auth", "chat", "delivery"):
                    await execute(
                        "up",
                        "-d",
                        "--force-recreate",
                        "--wait",
                        "--wait-timeout",
                        "120",
                        service,
                    )

                async def healthy():
                    return (await a.get("/auth/me")).status_code == 200 and (
                        await a.get("/dialogs")
                    ).status_code == 200

                await eventually(healthy)
                await opened(stack, url, origin, tokens[0])
                results.append("proxy-resolves-recreated-services")
            finally:
                await execute("up", "-d", "--wait", "--wait-timeout", "180")
        # Rotation and logout at the public boundary.
        old = a.cookies.get("messenger_refresh")
        refreshed = await a.post("/auth/refresh", json={})
        assert (
            refreshed.status_code == 200 and a.cookies.get("messenger_refresh") != old
        )
        assert (await a.post("/auth/logout", json={})).status_code == 204
        assert (await a.post("/auth/refresh", json={})).status_code == 401
        results.append("refresh-rotation-and-logout")
    report = {
        "status": "passed",
        "checks": results,
        "seconds": round(time.time() - started, 2),
        "message_id": message["message_id"],
        "dialog_id": dialog,
        "finished_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument("--faults", action="store_true")
    parser.add_argument("--output", type=Path, default=ROOT / "docs/step4/checks.json")
    parser.add_argument("--backend", choices=["compose", "kubernetes"], default="compose")
    args = parser.parse_args()
    if args.backend == "kubernetes":
        if args.faults:
            parser.error("--faults is Compose-only; no Compose mutation is allowed in Kubernetes mode")
        from step5_common import Cluster
        cluster = Cluster()
        cluster.verify()

        async def kubernetes(*command):
            if command[:3] == ("exec", "-T", "postgres"):
                return (await asyncio.to_thread(
                    cluster.call, "exec", "statefulset/postgres", "--", *command[3:]
                )).stdout
            if command == ("logs", "--no-color", "--tail", "1000", "consumer"):
                return (await asyncio.to_thread(
                    cluster.call, "logs", "deployment/consumer", "--tail=1000"
                )).stdout
            raise RuntimeError("Unsupported Kubernetes operation; no Compose fallback")

        asyncio.run(run(args, execute=kubernetes))
    else:
        asyncio.run(run(args))

