"""Manage only this workspace's container, validating the deployed manifest."""
import hashlib,json,pathlib,re,subprocess,sys
B=pathlib.Path(__file__).resolve().parent
spec=json.loads((B/'runtime.json').read_text());name=spec['container']
def cmd(args,timeout=120):
 p=subprocess.run(args,capture_output=True,text=True,timeout=timeout)
 if p.returncode:raise RuntimeError(p.stderr or p.stdout)
 return p.stdout.strip()
def inspect():
 p=subprocess.run(['docker','inspect',name],capture_output=True,text=True)
 return json.loads(p.stdout)[0] if p.returncode==0 else None
action=sys.argv[1]
if action=='start':
 assert cmd(['docker','image','inspect','--format','{{.Id}}',spec['image']])==spec['image_id'],'Image identity mismatch'
 digest=hashlib.sha256((B/'runtime.json').read_bytes()).hexdigest();current=inspect()
 if current and current['State']['Running']:
  assert current['Config']['Labels'].get('luz.spec_sha256')==digest,'Running configuration differs'
  print(json.dumps({'container':name,'reused':True,'id':current['Id']}));sys.exit(0)
 for other in cmd(['docker','ps','--format','{{.Names}}']).splitlines():
  assert not other.startswith(('sglang-','vllm-','dsv41-','luz-switch-')),f'Another GPU model is running: {other}'
 compute_pids=cmd(['nvidia-smi','--query-compute-apps=pid','--format=csv,noheader'])
 assert not compute_pids.strip(),f'GPU compute processes remain: {compute_pids}'
 for hca,address in spec['rails'].items():
  port=pathlib.Path('/sys/class/infiniband')/hca/'ports/1'
  assert 'ACTIVE' in (port/'state').read_text()
  assert (port/'gid_attrs/types/3').read_text().strip()=='RoCE v2'
  ipv4='.'.join(str(int((port/'gids/3').read_text().strip().replace(':','')[-8:][i:i+2],16)) for i in range(0,8,2))
  assert ipv4==address,(hca,ipv4,address)
 for interface in spec['interfaces']:
  assert (pathlib.Path('/sys/class/net')/interface/'mtu').read_text().strip()=='9000'
  qos=cmd(['mlnx_qos','-i',interface]);assert 'Priority trust state: dscp' in qos
  pfc=re.search(r'^\s*enabled\s+([01]\s+[01]\s+[01]\s+[01]\s+[01]\s+[01]\s+[01]\s+[01])',qos,re.M)
  assert pfc and pfc.group(1).split()==['0','0','0','1','0','0','0','0']
 for layer in [1,14]:assert (B/'engram'/f'engram-l{layer}-r{spec["rank"]}of4.bin').exists()
 memory=int(next(l.split()[1] for l in pathlib.Path('/proc/meminfo').read_text().splitlines() if l.startswith('MemAvailable:')))
 assert memory>=100_000_000, f'Insufficient available UMA: {memory} KiB'
 if '--dry-run' in sys.argv[2:]:
  print(json.dumps({'container':name,'preflight':True,'memory_kib':memory}));sys.exit(0)
 if current:cmd(['docker','rm',name])
 for d in ('state','cache','logs'):(B/d).mkdir(exist_ok=True)
 args=['docker','run','-d','--name',name,'--restart=no','--network','host','--ipc','host','--privileged','--cap-add','IPC_LOCK','--gpus','all','--memory','32g','--memory-swap','32g','--ulimit','memlock=-1:-1','--ulimit','stack=67108864','--device','/dev/infiniband:/dev/infiniband','--label','luz.workspace=luz-deepseek-flash-all-in-one-switch','--label','luz.spec_sha256='+digest]
 for src,dst,mode in spec['mounts']:args+=['-v',src+':'+dst+(':'+mode if mode else '')]
 for k,v in spec['env'].items():args+=['-e',k+'='+str(v)]
 args+=['--health-cmd',f'curl -fsS http://127.0.0.1:{spec["port"]}/health >/dev/null','--health-interval','30s','--health-timeout','10s','--health-start-period','1800s','--health-retries','6'] if spec['rank']==0 else []
 cid=cmd(args+[spec['image'],'python3','-m','sglang.launch_server']+spec['args'])
 print(json.dumps({'container':name,'id':cid,'rank':spec['rank']}))
elif action=='stop':
 c=inspect()
 if '--expected-id' in sys.argv:
  expected=sys.argv[sys.argv.index('--expected-id')+1]
  if c and c['Id']!=expected:
   print(json.dumps({'container':name,'stopped':False,'superseded':True}));sys.exit(0)
 if c and c['State']['Running']:cmd(['docker','stop','-t','30',c['Id']])
 print(json.dumps({'container':name,'stopped':True}))
elif action=='status':
 c=inspect();print(json.dumps({'container':name,'exists':bool(c),'state':c['State'] if c else None,'image':c['Image'] if c else None}))
elif action=='logs':print(cmd(['docker','logs','--tail',sys.argv[2] if len(sys.argv)>2 else '100',name]))
else:raise ValueError(action)
