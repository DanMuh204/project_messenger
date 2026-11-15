"""Local-only kind helpers; explicit context, no shell interpolation of input."""
import json
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CONTEXT = 'kind-messenger-step5'
NAMESPACE = 'messenger'
LABEL = 'app.kubernetes.io/component'


def run(command, *, input=None, check=True, timeout=600):
    result = subprocess.run(command, input=input, text=True, capture_output=True,
                            cwd=ROOT, timeout=timeout)
    if check and result.returncode:
        # Commands may process Secrets. Do not echo stdin, argv or raw output.
        raise RuntimeError(f'{command[0]} failed (exit {result.returncode}); inspect the target resource locally')
    return result


class Cluster:
    def __init__(self, context=CONTEXT):
        if context != CONTEXT:
            raise ValueError(f'This lab only operates on {CONTEXT}; production contexts are refused')
        self.context = context

    def cmd(self, *args):
        return ['kubectl', '--context', self.context, '-n', NAMESPACE, *args]

    def call(self, *args, **kwargs):
        return run(self.cmd(*args), **kwargs)

    def get(self, *args):
        return json.loads(self.call('get', *args, '-o', 'json').stdout)

    def apply(self, objects):
        if isinstance(objects, dict):
            objects = [objects]
        self.call('apply', '--server-side', '--field-manager=messenger-step5', '-f', '-',
                  input=json.dumps({'apiVersion':'v1','kind':'List','items':objects}))

    def pod(self, component):
        items = self.get('pods', '-l', f'{LABEL}={component}')['items']
        ready = [p for p in items if not p['metadata'].get('deletionTimestamp') and
                 any(c['type']=='Ready' and c['status']=='True'
                     for c in p.get('status',{}).get('conditions',[]))]
        if len(ready) != 1:
            raise RuntimeError(f'Expected one Ready pod for {component}, got {len(ready)}')
        return ready[0]

    def verify(self):
        nodes = self.get('nodes')['items']
        if len(nodes) != 1 or nodes[0]['metadata']['name'] != 'messenger-step5-control-plane':
            raise RuntimeError('Expected the dedicated single-node messenger-step5 kind cluster')
        self.call('rollout','status','daemonset/calico-node','-n','calico-system','--timeout=300s')
        self.call('rollout','status','deployment/coredns','-n','kube-system','--timeout=180s')


def documents(path):
    import yaml
    return list(yaml.safe_load_all((ROOT/path).read_text()))


def configmap(name, data):
    return {'apiVersion':'v1','kind':'ConfigMap',
            'metadata':{'name':name,'namespace':NAMESPACE,'labels':{'messenger-step':'5'}},'data':data}
