#!/usr/bin/env python3
"""Checksummed, transactionally tracked migrations. Uses psql inside Compose."""

import hashlib
from pathlib import Path
import subprocess

ROOT = Path(__file__).resolve().parents[1]


def migration_sql(name, content):
    checksum = hashlib.sha256(content.encode()).hexdigest()
    # Names are repository-controlled, content is sent as stdin, never shell code.
    # Keep our migration ledger separate from legacy ledgers that may have a
    # different schema in an existing auth_db or chat_db.
    return f"""\\set ON_ERROR_STOP on
BEGIN;
SELECT pg_advisory_xact_lock(410004);
CREATE TABLE IF NOT EXISTS messenger_step4_migrations (
    version text PRIMARY KEY, checksum text NOT NULL, applied_at timestamptz NOT NULL DEFAULT now()
);
SELECT EXISTS(SELECT 1 FROM messenger_step4_migrations WHERE version='{name}') AS applied,
       EXISTS(SELECT 1 FROM messenger_step4_migrations WHERE version='{name}' AND checksum<>'{checksum}') AS mismatch \\gset
\\if :mismatch
DO $$ BEGIN RAISE EXCEPTION 'Migration checksum mismatch: {name}'; END $$;
\\endif
\\if :applied
\\echo Already applied: {name}
\\else
{content}
INSERT INTO messenger_step4_migrations(version,checksum) VALUES('{name}','{checksum}');
\\endif
COMMIT;
"""


def main():
    subprocess.run(
        ["docker", "compose", "up", "-d", "--wait", "postgres"], cwd=ROOT, check=True
    )
    for db, role, password_var, files in [
        (
            "chat_db",
            "chat_app",
            "CHAT_DB_PASSWORD",
            ["001_chat_core.sql", "002_dialogs.sql"],
        ),
        ("auth_db", "auth_app", "AUTH_DB_PASSWORD", ["auth/001_auth.sql"]),
    ]:
        for name in files:
            content = (ROOT / "infra/postgres/migrations" / name).read_text()
            script = migration_sql(name, content)
            # The original chat migration is CREATE IF NOT EXISTS and also supports fresh installs.
            command = f'PGPASSWORD="${password_var}" exec psql -h 127.0.0.1 -U {role} -d {db} -v ON_ERROR_STOP=1'
            subprocess.run(
                ["docker", "compose", "exec", "-T", "postgres", "sh", "-c", command],
                cwd=ROOT,
                input=script,
                text=True,
                check=True,
            )


if __name__ == "__main__":
    main()
