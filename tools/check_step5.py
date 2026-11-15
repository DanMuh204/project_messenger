#!/usr/bin/env python3
"""Prove TCP filtering from real pods; a refused connection is NOT a successful deny."""
import argparse
import json
import sys
import time
from datetime import datetime, timezone
from uuid import uuid4
from step5_common import ROOT, LABEL, Cluster, run

# Positive controls target the same live listeners as negative attempts.
ALLOWED=[('chat','postgres',5432),('auth','postgres',5432),('outbox','postgres',5432),
         ('chat','auth',8000),('chat','redis',6379),('outbox','redis',6379),
         ('outbox','rabbitmq',5672),('delivery','rabbitmq',5672),('consumer','rabbitmq',5672)]
DENIED=[('delivery','postgres',5432),('delivery','redis',6379),('auth','redis',6379),
        ('auth','rabbitmq',5672),('consumer','postgres',5432),('chat','rabbitmq',5672)]
PROBE='''import json,socket,sys,time
host,port=sys.argv[1],int(sys.argv[2]); start=time.monotonic()
try:
    with socket.create_connection((host,port),timeout=3): pass
    status="connected"
except (socket.timeout,TimeoutError): status="timeout"
except socket.gaierror: status="dns_error"
except ConnectionRefusedError: status="refused"
except OSError: status="network_error"
print(json.dumps({"status":status,"seconds":round(time.monotonic()-start,3)}))
'''


def expectation(mode, allowed):
    return 'connected' if mode=='baseline' or allowed else 'timeout'


def probe(cluster, pod, host, port):
    # A kubectl/RBAC/exec failure raises; it must never count as a firewall deny.
    result=cluster.call('exec',pod,'--','python','-c',PROBE,host,str(port),timeout=20)
    return json.loads(result.stdout)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--mode',choices=['baseline','enforce'],default='enforce')
    parser.add_argument('--baseline',type=str,help='Compare application pod UIDs with a preceding baseline JSON')
    parser.add_argument('--application',action='store_true',help='Also run HTTP/SQL/WS acceptance; installs no software')
    parser.add_argument('--output',type=str)
    args=parser.parse_args()
    output=ROOT/(args.output or f'docs/step5/{args.mode}-live.json')
    report={'status':'failed','mode':args.mode,'started_at':datetime.now(timezone.utc).isoformat(),'checks':[]}
    c=Cluster(); unknown='step5-probe-'+uuid4().hex[:8]; created=False
    try:
        c.verify()
        policies=c.get('networkpolicy')['items']
        report['policy_names']=[p['metadata']['name'] for p in policies]
        if args.mode=='baseline' and policies:
            raise RuntimeError('Baseline requires no policies in messenger')
        if args.mode=='enforce' and not any(p['metadata']['name']=='default-deny' for p in policies):
            raise RuntimeError('default-deny is absent')
        names={x for edge in ALLOWED+DENIED for x in edge[:2]}
        pods={name:c.pod(name) for name in names}
        report['pods']={name:{'name':p['metadata']['name'],'uid':p['metadata']['uid'],
                              'ip':p['status']['podIP']} for name,p in pods.items()}
        report['images']={name:p['spec']['containers'][0]['image'] for name,p in pods.items()}
        # Kubernetes service links can inject RABBITMQ_PORT=tcp://...;
        # the Python broker clients expect the integer AMQP port 5672.
        live_pods = c.get('pods', '-l', 'app.kubernetes.io/part-of=messenger')['items']
        for item in live_pods:
            if item['spec'].get('enableServiceLinks') is not False:
                raise RuntimeError(
                    'Service environment injection enabled in Pod ' + item['metadata']['name']
                )
        report['service_links_disabled'] = len(live_pods)
        for role in ('outbox', 'consumer', 'delivery'):
            code = (
                'import json,os; '
                'print(json.dumps({"injected": "RABBITMQ_PORT" in os.environ, '
                '"port": os.getenv("RABBITMQ_PORT", "5672")}))'
            )
            result = c.call('exec', pods[role]['metadata']['name'],
                            '--', 'python', '-c', code)
            env = json.loads(result.stdout)
            if env['injected'] or env['port'] != '5672':
                raise RuntimeError(role + ': unexpected RABBITMQ_PORT environment variable')
        report['broker_environment'] = 'passed: AMQP clients default to numeric port 5672'
        report['context']=c.context
        report['kubernetes']=c.call('version','-o','json').stdout
        report['source_commit']=run(['git','rev-parse','HEAD']).stdout.strip()
        if args.baseline:
            base=json.loads((ROOT/args.baseline).read_text())
            if base.get('status')!='passed' or base.get('mode')!='baseline':
                raise RuntimeError('A successful baseline report is required')
            if base['pods'] != report['pods']:
                raise RuntimeError('Pods/IPs changed since baseline: cannot claim same-pod comparison')
        # Verify DNS independently so DNS failure cannot masquerade as a deny.
        for src in ('auth','chat','outbox','delivery','consumer'):
            c.call('exec',pods[src]['metadata']['name'],'--','python','-c',
                   "import socket; socket.getaddrinfo('postgres',5432)")
        report['dns']='passed for application Python processes'
        for allowed,edges in ((True,ALLOWED),(False,DENIED)):
            for src,dst,port in edges:
                for kind,host in [('service',dst),('pod-ip',pods[dst]['status']['podIP'])]:
                    # Every attempt creates a fresh connection; repeat negative attempts.
                    for attempt in range(1,2 if allowed or args.mode=='baseline' else 3):
                        result=probe(c,pods[src]['metadata']['name'],host,port)
                        expected=expectation(args.mode,allowed)
                        report['checks'].append({'source':src,'destination':dst,'target':host,
                            'address_type':kind,'port':port,'attempt':attempt,'expected':expected,
                            **result,'passed':result['status']==expected})
        # Unlabelled workload: default-deny applies even without any role label.
        c.apply({'apiVersion':'v1','kind':'Pod','metadata':{'name':unknown,'namespace':'messenger',
                 'labels':{'messenger-step':'5-probe'}},'spec':{'automountServiceAccountToken':False,'enableServiceLinks':False,
                 'restartPolicy':'Never','containers':[{'name':'probe','image':pods['chat']['spec']['containers'][0]['image'],
                 'imagePullPolicy':'IfNotPresent','command':['python','-c','import time; time.sleep(300)'],
                 'resources':{'requests':{'cpu':'10m','memory':'16Mi'},'limits':{'cpu':'100m','memory':'64Mi'}}}]}})
        created=True
        c.call('wait','--for=condition=Ready','pod/'+unknown,'--timeout=90s')
        result=probe(c,unknown,pods['postgres']['status']['podIP'],5432)
        expected=expectation(args.mode,False)
        report['checks'].append({'source':'unlabelled','destination':'postgres','port':5432,
                                 'expected':expected,**result,'passed':result['status']==expected})
        # Positive DB control after all negative attempts proves the listener did not go away.
        control=probe(c,pods['chat']['metadata']['name'],pods['postgres']['status']['podIP'],5432)
        report['checks'].append({'source':'chat','destination':'postgres','phase':'final-control',
                                'expected':'connected',**control,'passed':control['status']=='connected'})
        if not all(row['passed'] for row in report['checks']):
            raise RuntimeError('Network matrix mismatch; inspect report (refused/DNS failures are inconclusive)')
        if args.application:
            app_output=output.with_name(output.stem+'-app-live.json')
            run([sys.executable,'tools/check_step4.py','--backend','kubernetes','--output',str(app_output)],timeout=180)
            report['application_report']=str(app_output.relative_to(ROOT))
        report['status']='passed'
    except Exception as exc:
        report['error']=str(exc)
    finally:
        if created:
            try: c.call('delete','pod',unknown,'--wait=false')
            except Exception: report['cleanup_warning']='Remove temporary pod '+unknown
        report['finished_at']=datetime.now(timezone.utc).isoformat()
        output.parent.mkdir(parents=True,exist_ok=True)
        output.write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n')
    print(f"{report['status']}: {output}")
    return 0 if report['status']=='passed' else 1


if __name__=='__main__':
    sys.exit(main())
