#!/usr/bin/env python3
"""Deploy to dedicated kind-messenger-step5. Never deletes clusters, PVCs or Secrets."""
import argparse
import base64
import hashlib
import json
import secrets
import sys
from pathlib import Path
from step5_common import ROOT, Cluster, configmap, documents, run
from migrate_step4 import migration_sql

SECRET_NAMES = ('postgres-admin','chat-db','auth-db','broker','auth-private','auth-internal')


def apply_policies(cluster, mode):
    current = cluster.get('networkpolicy')['items']
    foreign = [p['metadata']['name'] for p in current
               if p['metadata'].get('labels',{}).get('messenger-step') != '5']
    if foreign:
        raise RuntimeError('Unexpected policies in lab namespace: '+', '.join(foreign))
    if mode == 'baseline':
        if current:
            raise RuntimeError('Baseline is only allowed before any policies exist. Do not weaken a running deployment.')
    else:
        cluster.apply(documents('infra/k8s/network-policies/policies.yaml'))


def prepare_secrets(cluster, output):
    found = {s['metadata']['name']:s for s in cluster.get('secrets')['items']
             if s['metadata']['name'] in SECRET_NAMES}
    public = cluster.call('get','configmap','auth-public','-o','json',check=False)
    if found:
        if set(found) != set(SECRET_NAMES) or public.returncode:
            raise RuntimeError('Partial credentials found. Restore missing Secrets/public key; refusing to rotate credentials.')
        required = {'auth-private':'auth-private.pem','auth-internal':'internal-auth.secret'}
        for name in SECRET_NAMES:
            if required.get(name,'password') not in found[name].get('data',{}):
                raise RuntimeError('Credential format mismatch: '+name)
        # Preserve all credentials on repeat runs; export an identical recovery bundle locally.
        from cryptography.hazmat.primitives import serialization
        key = serialization.load_pem_private_key(base64.b64decode(found['auth-private']['data']['auth-private.pem']),None)
        expected = key.public_key().public_bytes(serialization.Encoding.PEM,serialization.PublicFormat.SubjectPublicKeyInfo).decode()
        cm = json.loads(public.stdout)
        if cm['data']['auth-public.pem'] != expected:
            raise RuntimeError('Auth public/private keys do not match')
        bundle = [*found.values(), cm]
    else:
        # Existing PVCs without credentials must never be silently initialized with new passwords.
        if cluster.get('pvc')['items'] or not public.returncode:
            raise RuntimeError('Existing data/public key without complete Secrets: restore credentials before deploying')
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric import rsa
        key = rsa.generate_private_key(public_exponent=65537,key_size=2048)
        values = {name:{'password':secrets.token_urlsafe(32)} for name in SECRET_NAMES[:4]}
        values['auth-private']={'auth-private.pem':key.private_bytes(serialization.Encoding.PEM,serialization.PrivateFormat.PKCS8,serialization.NoEncryption()).decode()}
        values['auth-internal']={'internal-auth.secret':secrets.token_urlsafe(48)}
        bundle = [{'apiVersion':'v1','kind':'Secret','metadata':{'name':name,'namespace':'messenger'},
                   'type':'Opaque','data':{k:base64.b64encode(v.encode()).decode() for k,v in data.items()}}
                  for name,data in values.items()]
        bundle.append(configmap('auth-public',{'auth-public.pem':key.public_key().public_bytes(serialization.Encoding.PEM,serialization.PublicFormat.SubjectPublicKeyInfo).decode()}))
    # Strip server fields from recovery export. Never print or commit this bundle.
    clean = []
    for obj in bundle:
        clean.append({k:v for k,v in obj.items() if k in ('apiVersion','kind','data','type')})
        clean[-1]['metadata']={'name':obj['metadata']['name'],'namespace':'messenger'}
    clean.sort(key=lambda obj: (obj['kind'],obj['metadata']['name']))
    output.mkdir(mode=0o700,parents=True,exist_ok=True)
    output.chmod(0o700)
    dest=output/'credentials.json'
    if dest.exists():
        previous = json.loads(dest.read_text())
        if previous != {'apiVersion':'v1','kind':'List','items':clean}:
            raise RuntimeError('Local recovery bundle belongs to different credentials; archive it securely before proceeding')
    # Open with 0600 before writing bytes, including on first creation.
    import os
    with os.fdopen(os.open(dest,os.O_WRONLY|os.O_CREAT|os.O_TRUNC,0o600),'w') as f:
        os.chmod(dest,0o600)
        json.dump({'apiVersion':'v1','kind':'List','items':clean},f,indent=2)
    if not found:
        cluster.apply(clean)


def configs(cluster):
    dns=cluster.get('service','kube-dns','-n','kube-system')['spec']['clusterIP']
    proxy=(ROOT/'infra/haproxy/haproxy-k8s.cfg').read_text().replace('__CLUSTER_DNS__',dns)
    sql={}
    for name in ('001_chat_core.sql','002_dialogs.sql','auth/001_auth.sql'):
        content=(ROOT/'infra/postgres/migrations'/name).read_text()
        key=('auth-' if name.startswith('auth/') else 'chat-')+Path(name).name
        sql[key]=migration_sql(name,content)
    cluster.apply([configmap('haproxy-k8s',{'haproxy.cfg':proxy}),
                   configmap('postgres-init',{'01-databases.sql':(ROOT/'infra/postgres/init/01-databases.sql').read_text()}),
                   configmap('migrations-step5',sql)])
    return hashlib.sha256(proxy.encode()).hexdigest()


def migrate(cluster):
    cluster.call('delete','job','migrate-step5','--ignore-not-found=true','--wait=true')
    cluster.apply(documents('infra/k8s/jobs/migrate.yaml'))
    cluster.call('wait','--for=condition=complete','job/migrate-step5','--timeout=300s')


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--network-mode',choices=['baseline','enforce'],default='enforce')
    parser.add_argument('--policies-only',action='store_true')
    parser.add_argument('--migrate-only',action='store_true')
    parser.add_argument('--build',action='store_true',help='Build and load application images into the local kind cluster')
    parser.add_argument('--tag',default='step5')
    args=parser.parse_args()
    import re
    if not re.fullmatch(r'[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}',args.tag):
        parser.error('Invalid image tag')
    if args.policies_only and args.migrate_only:
        parser.error('Select only one operation')
    if args.policies_only and args.network_mode != 'enforce':
        parser.error('--policies-only requires enforce')
    c=Cluster(); c.verify()
    c.apply(documents('infra/k8s/base/namespace.yaml'))
    if args.policies_only:
        apply_policies(c,'enforce'); print('Network policies applied; run check_step5.py'); return
    if args.migrate_only:
        migrate(c); print('Migrations completed'); return
    apply_policies(c,args.network_mode)
    if args.network_mode=='baseline':
        print('BASELINE: pod network is unrestricted until --policies-only; local disposable lab only.')
    prepare_secrets(c,ROOT/'dev-secrets/step5')
    proxy_hash=configs(c)
    if args.build:
        for name in ('chat','auth','delivery'):
            image=f'messenger-{name}:{args.tag}'
            print('Building/loading '+image,flush=True)
            run(['docker','build','-t',image,str(ROOT/'services'/name)],timeout=1200)
            run(['kind','load','docker-image','--name','messenger-step5',image],timeout=300)
    c.apply(documents('infra/k8s/base/services.yaml'))
    c.apply(documents('infra/k8s/base/infrastructure.yaml'))
    for resource in ('statefulset/postgres','statefulset/rabbitmq','deployment/redis'):
        c.call('rollout','status',resource,'--timeout=300s')
    migrate(c)
    apps=documents('infra/k8s/base/apps.yaml')
    for app in apps:
        pod=app['spec']['template']
        for container in pod['spec']['containers']:
            if container['image'].startswith('messenger-'):
                container['image']=container['image'].replace(':step5',':'+args.tag)
        if app['metadata']['name']=='proxy':
            pod['metadata']['annotations']={'messenger/config-hash':proxy_hash}
    c.apply(apps)
    for name in ('auth','chat','delivery','outbox','consumer','proxy'):
        c.call('rollout','status','deployment/'+name,'--timeout=300s')
    print('Deployed: http://127.0.0.1:8000 ; run network and application checks next.')


if __name__=='__main__':
    try: main()
    except (RuntimeError, ValueError) as exc:
        sys.exit(str(exc))
