"""One 600K-class prompt and its prefix-hit replay; no full-C16 test."""
import argparse,datetime,hashlib,json,pathlib,random,time,urllib.request
p=argparse.ArgumentParser();p.add_argument('--output',required=True);p.add_argument('--target',type=int,default=599500)
a=p.parse_args();assert 590000<=a.target<=599500
dest=pathlib.Path(a.output);dest.mkdir(parents=True,exist_ok=False)
deadline=time.time()+1800
op=urllib.request.build_opener(urllib.request.ProxyHandler({}));base='http://127.0.0.1:8006'
def api(path,body=None,timeout=900):
    assert time.time()<deadline,'Long-context check cutoff reached'
    req=urllib.request.Request(base+path,None if body is None else json.dumps(body).encode(),{'Content-Type':'application/json'})
    with op.open(req,timeout=min(timeout,max(1,deadline-time.time()))) as r:
        return r.read().decode() if path=='/metrics' else json.load(r)
info=api('/server_info');assert info['context_length']==600000 and info['max_total_num_tokens']==9600000 and info['max_running_requests']==16
for key in ['api_key','admin_api_key','ssl_keyfile_password']:info.pop(key,None)
(dest/'server-info.json').write_text(json.dumps(info,indent=2))
words=('time year people way day thing world school state family student group country problem hand part place case week company system program question work number night point home water room area money story fact month book eye job word business issue side kind head house service friend power hour game line end member city community name team minute idea body information back parent face level office door person art history party result change morning reason research moment air teacher force education foot age').split()
sequence=random.Random(15102026).choices(words,k=650000)
codes=['LUZ_A_58691327','LUZ_B_49187263','LUZ_C_73862149']
def make(n):
    parts=[];last=0
    for label,fraction,code in zip('ABC',[.1,.5,.9],codes):
        pos=int(n*fraction);parts.append(' '.join(sequence[last:pos]));parts.append(f'\nUnique record {label}: {code}.\n');last=pos
    parts.append(' '.join(sequence[last:n]))
    return 'Read this archive. Find the three unique record values.\n'+'\n'.join(parts)+'\nReturn the exact values of unique records A, B and C, in that order. Return only those values.'
n=a.target;tokenization=[]
for attempt in range(7):
    text=make(n);res=api('/tokenize',{'model':'deepseek-v4.1-flash-ul-luz','prompt':text},90)
    tokens=next((res[k] for k in ['count','num_tokens','token_count'] if isinstance(res.get(k),int)),None)
    if tokens is None:tokens=len(res['input_ids'])
    tokenization.append({'words':n,'plain_text_tokens':tokens});print(json.dumps(tokenization[-1]),flush=True)
    if a.target-256<=tokens<=a.target:break
    n=min(len(sequence),max(1,int(n*(a.target-64)/tokens)))
else:raise AssertionError('Could not size the prompt')
(dest/'tokenization.json').write_text(json.dumps(tokenization,indent=2))
(dest/'prompt.txt').write_text(text)
records=[]
for mode in ['cold','prefix_hit']:
    before=api('/metrics');started=time.time()
    response=api('/v1/chat/completions',{'model':'deepseek-v4.1-flash-ul-luz','messages':[{'role':'user','content':text}],
                 'temperature':0,'max_tokens':128,'chat_template_kwargs':{'thinking':False}})
    answer=response['choices'][0]['message'].get('content') or '';usage=response['usage']
    record={'round':mode,'elapsed_s':time.time()-started,'usage':usage,'answer':answer,
            'three_positions_recalled':all(code in answer for code in codes),'prompt_sha256':hashlib.sha256(text.encode()).hexdigest()}
    records.append(record);(dest/'results.json').write_text(json.dumps(records,ensure_ascii=False,indent=2))
    (dest/(mode+'-metrics-before.txt')).write_text(before);(dest/(mode+'-metrics-after.txt')).write_text(api('/metrics'))
    print(json.dumps(record,ensure_ascii=False),flush=True)
    assert 590000<=usage['prompt_tokens']<600000 and record['three_positions_recalled'],record
(dest/'complete.json').write_text(json.dumps({'passed':True,'rounds':2,'independent_long_streams':1,'full_c16_qualified':False}))
