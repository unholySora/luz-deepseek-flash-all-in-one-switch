"""Native API workload only; plateau release is controlled by independent monitors."""
import argparse,concurrent.futures,hashlib,json,pathlib,sys,time,urllib.request,urllib.error,uuid,traceback
from run_kv_recall import grade
p=argparse.ArgumentParser();p.add_argument('--suite',required=True);p.add_argument('--output',required=True);p.add_argument('--stage',required=True);p.add_argument('--prefill-limit',type=int,default=0);a=p.parse_args()
suite=pathlib.Path(a.suite);out=pathlib.Path(a.output);out.mkdir(parents=True,exist_ok=False)
m=json.loads((suite/'manifest.json').read_text());cases=[]
for r in m['cases']:
 data=(suite/r['file']).read_bytes();assert hashlib.sha256(data).hexdigest()==r['sha256'];cases.append(json.loads(data))
prefix='kvfull-'+uuid.uuid4().hex
opened=[];records=[]
def write(name,value):
 dest=out/name;tmp=dest.with_suffix('.tmp');tmp.write_text(json.dumps(value,ensure_ascii=False,indent=2)+'\n');tmp.replace(dest)
def status(phase,**values):write('status.json',{'phase':phase,'epoch':time.time(),**values})
def api(path,body=None,raw=False,timeout=1800):
 op=urllib.request.build_opener(urllib.request.ProxyHandler({}))
 req=urllib.request.Request('http://127.0.0.1:8006'+path,None if body is None else json.dumps(body).encode(),{'Content-Type':'application/json'})
 try:
  with op.open(req,timeout=timeout) as r:data=r.read().decode()
 except urllib.error.HTTPError as e:
  detail=e.read().decode(errors='replace');write('http-error-'+str(time.time_ns())+'.json',{'path':path,'status':e.code,'body':detail})
  raise RuntimeError('HTTP '+str(e.code)+' '+path+': '+detail) from e
 return data if raw else json.loads(data)
def phase(name,**values):
 status(name,**values)
 deadline=time.time()+180
 while not (out/('release-'+name)).exists():
  if (out/'abort').exists():raise RuntimeError('Controller aborted stage')
  if time.time()>deadline:raise TimeoutError('No independent monitor release at '+name)
  time.sleep(.25)
def metrics(name):
 try:(out/(name+'-metrics.txt')).write_text(api('/metrics',raw=True,timeout=20))
 except Exception as e:write(name+'-metric-error.json',{'type':type(e).__name__})
try:
 info=api('/server_info',timeout=20);assert (info['context_length'],info['max_total_num_tokens'],info['max_running_requests'])==(600000,9600000,16)
 assert info.get('enable_streaming_session') is True,'Test profile must launch with --enable-streaming-session'
 if a.prefill_limit:assert info.get('prefill_max_requests')==a.prefill_limit,'Prefill admission cap does not match test profile'
 write('execution-profile.json',{k:info.get(k) for k in ['context_length','max_total_num_tokens','max_running_requests','enable_streaming_session','prefill_max_requests']})
 write('owned-request-prefix.json',{'rid_prefix':prefix})
 write('suite-manifest.json',m);phase('before')
 metrics('before')
 for c in cases:
  sid=prefix+'-'+str(c['stream']);session=api('/open_session',{'session_id':sid,'streaming':True,'capacity_of_str_len':c['text_chars']+100000,'timeout':10800},timeout=30)
  assert session==sid or (isinstance(session,dict) and session.get('session_id')==sid and session.get('success',True)),session
  opened.append(sid);start=time.time();status('filling',filled=len(opened)-1,active_session=sid,stream=c['stream'],start_epoch=start)
  response=api('/generate',{'rid':sid+'-fill','input_ids':c['input_ids'],'session_params':{'id':sid},'sampling_params':{'temperature':0,'max_new_tokens':32},'stream':False})
  write('fill-%02d.json'%c['stream'],response)
  meta=response.get('meta_info',{});ack=response.get('text','').strip()
  assert ack=='ACK' and meta.get('finish_reason',{}).get('type')=='stop',{'ack':ack,'meta':meta}
  assert meta['prompt_tokens']==c['input_tokens']
  print(json.dumps({'stage':a.stage,'filled':len(opened),'total':len(cases),'elapsed_s':time.time()-start}),flush=True)
 positions={sid:c['record_token_positions'] for sid,c in zip(opened,cases)}
 phase('plateau',sessions=opened,positions=positions,input_tokens={sid:c['input_tokens'] for sid,c in zip(opened,cases)})
 metrics('plateau')
 values={}
 for c in cases:
  for k,v in c['expected'].items():values.setdefault(k,set()).add(v)
 def query(pair):
  sid,c=pair;started=time.time()
  response=api('/generate',{'rid':sid+'-query','input_ids':c['query_ids'],'session_params':{'id':sid},'sampling_params':{'temperature':0,'max_new_tokens':512},'stream':False})
  write('query-%02d.json'%c['stream'],response)
  other={k:v-{c['expected'][k]} for k,v in values.items()}
  g=grade(c['expected'],response.get('text',''),other)
  try:answer=json.loads(response.get('text',''))
  except Exception:answer={}
  per_position=[{'key':k,'position':pos,'correct':isinstance(answer,dict) and answer.get(k)==v,
                 'wrong_stream':isinstance(answer,dict) and isinstance(answer.get(k),str) and answer[k] in other[k]}
                for (k,v),pos in zip(c['expected'].items(),c['record_token_positions'])]
  return {'session':sid,'stream':c['stream'],'elapsed_s':time.time()-started,'grade':g,'positions':per_position,'meta_info':response.get('meta_info',{})}
 status('querying',sessions=opened)
 with concurrent.futures.ThreadPoolExecutor(len(cases)) as pool:
  for record in pool.map(query,zip(opened,cases)):records.append(record);write('records.json',records)
 phase('after-query',sessions=opened,positions=positions)
 total=sum(r['grade']['total'] for r in records);correct=sum(r['grade']['correct'] for r in records)
 summary={'stage':a.stage,'streams':len(cases),'input_tokens':sum(c['input_tokens'] for c in cases),
          'correct':correct,'total':total,'record_recall':correct/total,'streams_all_correct':sum(r['grade']['exact'] for r in records),
          'wrong_stream':sum(r['grade']['wrong_stream'] for r in records),'format_errors':sum(not r['grade']['json_valid'] for r in records),
          'per_position':[{'position_fraction':f,'correct':sum(r['positions'][i]['correct'] for r in records),'total':len(cases),'wrong_stream':sum(r['positions'][i]['wrong_stream'] for r in records)} for i,f in enumerate([.01,.1,.25,.5,.75,.9,.99])],
          'residency_independently_verified':False,'full_pool_qualified':False}
 write('summary.json',summary)
except Exception:
 write('error.json',{'traceback':traceback.format_exc(),'epoch':time.time()});status('failed');raise
finally:
 cleanup=[]
 if (out/'error.json').exists():
  try:api('/abort_request',{'rid':prefix},raw=True,timeout=15)
  except Exception:pass
 for sid in opened:
  try:api('/close_session',{'session_id':sid},raw=True,timeout=30);cleanup.append({'session':sid,'close_requested':True})
  except Exception as e:cleanup.append({'session':sid,'close_requested':False,'error':type(e).__name__})
 write('cleanup.json',cleanup)
 if not (out/'error.json').exists():
  phase('closed',sessions=opened);write('complete.json',{'workload_finished':True,'epoch':time.time()});status('complete')
