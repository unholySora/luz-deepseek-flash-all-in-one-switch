"""Head-side cluster start/stop with pinned runtime manifests."""
import concurrent.futures,json,pathlib,subprocess,sys,time,urllib.request
B=pathlib.Path(__file__).resolve().parent
config=json.loads((B/'cluster.json').read_text());action=sys.argv[1]
def run(node,mode,expected_id=None):
 argv=['python3',node['path']+'/node.py',mode]
 if expected_id is not None:argv+=['--expected-id',expected_id]
 if node.get('sudo',False):argv=['sudo','-n']+argv
 if node['rank']!=0:argv=['ssh','-i',str(B/'ssh/id_ed25519'),'-o','UserKnownHostsFile='+str(B/'ssh/known_hosts'),'-o','StrictHostKeyChecking=yes','-o','BatchMode=yes','-o','ConnectTimeout=8',node['ssh']]+argv
 p=subprocess.run(argv,capture_output=True,text=True,timeout=150)
 if p.returncode:raise RuntimeError(node['name']+': '+(p.stderr or p.stdout))
 return {'node':node['name'],'result':json.loads(p.stdout)}
nodes=config['nodes'];op=urllib.request.build_opener(urllib.request.ProxyHandler({}))
if action in ('status','stop'):
 with concurrent.futures.ThreadPoolExecutor(4) as pool:print(json.dumps(list(pool.map(lambda n:run(n,action),nodes))))
elif action=='start':
 started={}
 def launch(node):
  result=run(node,'start');started[node['name']]=result['result']['id'];return result
 try:
  with concurrent.futures.ThreadPoolExecutor(3) as pool:list(pool.map(launch,[n for n in nodes if n['rank']!=0]))
  launch(next(n for n in nodes if n['rank']==0))
  deadline=time.monotonic()+2400
  while time.monotonic()<deadline:
   try:
    with op.open(config['url']+'/health',timeout=10) as r:healthy=r.status==200
    if healthy:
     info=json.load(op.open(config['url']+'/server_info',timeout=15))
     assert info['max_total_num_tokens']>=9600000,info['max_total_num_tokens']
     desired=json.loads((B/'runtime.json').read_text())['args'];assert info['context_length']==int(desired[desired.index('--context-length')+1]) and info['max_running_requests']==int(desired[desired.index('--max-running-requests')+1])
     models=json.load(op.open(config['url']+'/v1/models',timeout=15))
     assert any(m['id']=='deepseek-v4.1-flash-ul-luz' for m in models['data'])
     print(json.dumps({'healthy':True,'max_total_num_tokens':info['max_total_num_tokens'],'context_length':info['context_length'],'max_running_requests':info['max_running_requests']}));break
   except (AssertionError,):raise
   except Exception:pass
   time.sleep(10)
  else:raise TimeoutError('Cluster did not become healthy')
 except Exception:
  # A superseded start must never tear down a newer experimental boot.
  with concurrent.futures.ThreadPoolExecutor(4) as pool:list(pool.map(lambda n:run(n,'stop',started[n['name']]),[n for n in nodes if n['name'] in started]))
  raise
else:raise ValueError(action)
