"""Bounded functional and real concurrent-request acceptance against the head."""
import argparse,base64,concurrent.futures,datetime,json,pathlib,re,struct,threading,time,urllib.request,zlib
B=pathlib.Path(__file__).resolve().parent
parser=argparse.ArgumentParser();parser.add_argument('--output',type=pathlib.Path,default=B/'acceptance')
parser.add_argument('--base',default='http://127.0.0.1:8006');parser.add_argument('--model',default='deepseek-v4.1-flash-ul-luz')
parser.add_argument('--context-length',type=int,default=600000)
parser.add_argument('--expect-running-limit',type=int,default=16);parser.add_argument('--min-token-pool',type=int,default=9600000)
parser.add_argument('--concurrency',type=int,default=8)
parser.add_argument('--sustained-chat',action='store_true',help='Use diverse chat topics for the sustained concurrency check')
args=parser.parse_args()
out=args.output;out.mkdir(parents=True,exist_ok=True)
base=args.base.rstrip('/');model=args.model;concurrency=args.concurrency;assert 1<=concurrency<=8
op=urllib.request.build_opener(urllib.request.ProxyHandler({}));records=[]
deadline=time.time()+1800
def api(path,body=None,timeout=600):
 assert time.time()<deadline,'Functional check cutoff reached'
 req=urllib.request.Request(base+path,None if body is None else json.dumps(body).encode(),{'Content-Type':'application/json'})
 with op.open(req,timeout=min(timeout,max(1,deadline-time.time()))) as response:
  return response.read().decode() if path=='/metrics' else json.load(response)
def chat(text,**extra):
 body={'model':model,'messages':[{'role':'user','content':text}],'temperature':0,'max_tokens':128,'chat_template_kwargs':{'thinking':False}};body.update(extra)
 started=time.monotonic();response=api('/v1/chat/completions',body)
 return {'seconds':time.monotonic()-started,'response':response}
def content(record):return record['response']['choices'][0]['message'].get('content') or ''
def check(name,fn,predicate):
 result=fn()
 try:passed=bool(predicate(result));error=None
 except Exception as exc:passed=False;error=repr(exc)
 records.append({'name':name,'passed':passed,'result':result,'predicate_error':error})
 (out/'functional.json').write_text(json.dumps(records,ensure_ascii=False,indent=2))
 print(json.dumps({'check':name,'passed':passed}),flush=True)
 assert passed,(name,result)
info=api('/server_info');assert info['context_length']==args.context_length and info['max_running_requests']==args.expect_running_limit and info['max_total_num_tokens']>=args.min_token_pool
for key in ['api_key','admin_api_key','ssl_keyfile_password']:info.pop(key,None)
(out/'server-info.json').write_text(json.dumps(info,indent=2))
check('arithmetic',lambda:chat('计算 17+25，只输出整数。'),lambda r:content(r).strip()=='42')
check('thinking',lambda:chat('计算17+25，只输出整数。',max_tokens=1024,chat_template_kwargs={'thinking':True},reasoning_effort='low'),lambda r:'42' in content(r) and bool(r['response']['choices'][0]['message'].get('reasoning_content')))
def stream():
 body={'model':model,'messages':[{'role':'user','content':'计算19+24，只输出整数。'}],'temperature':0,'max_tokens':64,'stream':True,'chat_template_kwargs':{'thinking':False}}
 req=urllib.request.Request(base+'/v1/chat/completions',json.dumps(body).encode(),{'Content-Type':'application/json'});text='';done=False
 with op.open(req,timeout=min(180,max(1,deadline-time.time()))) as response:
  for raw in response:
   assert time.time()<deadline,'SSE functional check cutoff reached'
   line=raw.decode().strip()
   if line=='data: [DONE]':done=True;break
   if line.startswith('data: '):
    for choice in json.loads(line[6:]).get('choices',[]):text+=choice.get('delta',{}).get('content') or ''
 return {'content':text,'done':done}
check('stream',stream,lambda r:r['content'].strip()=='43' and r['done'])
tools=[{'type':'function','function':{'name':'add','description':'Add two integers.','parameters':{'type':'object','properties':{'a':{'type':'integer'},'b':{'type':'integer'}},'required':['a','b'],'additionalProperties':False}}}]
check('tool',lambda:chat('调用add计算17+25。',tools=tools,tool_choice={'type':'function','function':{'name':'add'}}),lambda r:json.loads(r['response']['choices'][0]['message']['tool_calls'][0]['function']['arguments'])=={'a':17,'b':25})
schema={'type':'object','properties':{'answer':{'type':'integer'}},'required':['answer'],'additionalProperties':False}
check('json_schema',lambda:chat('输出17+25的答案，字段answer。',response_format={'type':'json_schema','json_schema':{'name':'result','strict':True,'schema':schema}}),lambda r:json.loads(content(r))=={'answer':42})
def chunk(kind,data):return struct.pack('!I',len(data))+kind+data+struct.pack('!I',zlib.crc32(kind+data)&0xffffffff)
png=b'\x89PNG\r\n\x1a\n'+chunk(b'IHDR',struct.pack('!IIBBBBB',256,256,8,2,0,0,0))+chunk(b'IDAT',zlib.compress((b'\0'+bytes([0,200,0])*256)*256))+chunk(b'IEND',b'')
uri='data:image/png;base64,'+base64.b64encode(png).decode()
check('vision',lambda:chat([{'type':'text','text':'这张图的主要颜色是什么？只回答颜色。'},{'type':'image_url','image_url':{'url':uri}}]),lambda r:'绿' in content(r) or 'green' in content(r).lower())
check('medium_context',lambda:chat(('Background record: the archive contains ordinary weather and travel notes.\n'*4500)+'\n计算17+25，只输出整数。'),lambda r:content(r).strip()=='42' and r['response']['usage']['prompt_tokens']>32768)
def arithmetic_concurrent():
 barrier=threading.Barrier(concurrency)
 def one(i):barrier.wait();return chat(f'计算{i}乘以7，只输出整数。')
 with concurrent.futures.ThreadPoolExecutor(concurrency) as pool:results=list(pool.map(one,range(21,21+concurrency)))
 return {'results':results}
check(f'correct_concurrent{concurrency}',arithmetic_concurrent,lambda r:all(content(x).strip()==str(i*7) for i,x in enumerate(r['results'],21)))
def sustained_concurrent():
 barrier=threading.Barrier(concurrency);maximum=0
 topics=['how hash maps handle collisions and resizing','how a camera forms an image on a sensor',
         'why yeast makes bread rise','how TCP recovers from a lost packet',
         'how a bicycle stays stable while moving','how plant roots absorb water',
         'how a database transaction maintains consistency','how a symphony orchestra rehearses a new piece',
         'how a refrigerator moves heat','how a compiler translates source code',
         'how astronomers measure the distance to stars','how a library preserves old paper documents']
 def one(i):
  barrier.wait()
  if args.sustained_chat:
   r=api('/v1/chat/completions',{'model':model,'messages':[{'role':'user','content':f'Explain {topics[i%len(topics)]} in detailed English prose, with concrete examples and several paragraphs.'}],'temperature':0,'max_tokens':256,'min_tokens':256,'ignore_eos':True,'chat_template_kwargs':{'thinking':False}},timeout=600)
   return {'meta_info':{'completion_tokens':r['usage']['completion_tokens'],'finish_reason':r['choices'][0]['finish_reason']},'content_prefix':(r['choices'][0]['message'].get('content') or '')[:200]}
  return api('/generate',{'text':f'Record {i}: Write a detailed encyclopedia entry about the history of mathematics.','sampling_params':{'temperature':0,'max_new_tokens':256,'ignore_eos':True}},timeout=600)
 with concurrent.futures.ThreadPoolExecutor(concurrency) as pool:
  futures=[pool.submit(one,i) for i in range(concurrency)]
  while not all(f.done() for f in futures):
   metrics=api('/metrics',timeout=15)
   values=[float(v) for v in re.findall(r'^sglang:num_running_reqs(?:\{[^\n]*\})?\s+([0-9.e+\-]+)',metrics,re.M)]
   if values:maximum=max(maximum,max(values))
   time.sleep(.15)
  results=[f.result() for f in futures]
 return {'maximum_running_observed':maximum,'request_style':'diverse_chat' if args.sustained_chat else 'raw_history_math','results':[{'completion_tokens':r.get('meta_info',{}).get('completion_tokens'),'finish_reason':r.get('meta_info',{}).get('finish_reason'),'content_prefix':r.get('content_prefix')} for r in results]}
check(f'sustained_running{concurrency}',sustained_concurrent,lambda r:r['maximum_running_observed']==concurrency and all(x['completion_tokens']==256 for x in r['results']))
(out/'complete.json').write_text(json.dumps({'passed':True,'checks':len(records)}))
