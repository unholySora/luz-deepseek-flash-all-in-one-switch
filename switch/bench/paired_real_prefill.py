"""Paired generated-text PR, reusing the pinned public instrument's SSE client."""
import argparse
import hashlib
import importlib.util
import json
import pathlib
import time
import urllib.request

p=argparse.ArgumentParser()
p.add_argument('--base',required=True)
p.add_argument('--model',required=True,choices=['luz','hybrid_v2'])
p.add_argument('--output',type=pathlib.Path,required=True)
p.add_argument('--prompts',type=pathlib.Path,required=True)
p.add_argument('--instrument',type=pathlib.Path,required=True)
a=p.parse_args()
a.output.mkdir(parents=True,exist_ok=True)
assert not (a.output/'results.json').exists() and not (a.output/'complete.json').exists()
suite=json.loads(a.prompts.read_text())
digest=lambda path:hashlib.sha256(path.read_bytes()).hexdigest()
assert digest(a.instrument)==suite['generator_sha256']
spec=importlib.util.spec_from_file_location('pinned_public_sdbench',a.instrument)
instrument=importlib.util.module_from_spec(spec)
spec.loader.exec_module(instrument)
client=instrument.Client(a.base,None,300)
op=urllib.request.build_opener(urllib.request.ProxyHandler({}))
server=a.base.removesuffix('/v1')
def get(path):
    with op.open(server+path,timeout=10) as f:return f.read().decode()
def idle():
    deadline=time.monotonic()+120
    while time.monotonic()<deadline:
        metrics=get('/metrics')
        active=sum(float(s.rsplit(' ',1)[1]) for s in metrics.splitlines()
                   if s.startswith(('sglang:num_running_reqs{','sglang:num_queue_reqs{')))
        if active==0:return metrics
        time.sleep(.25)
    raise TimeoutError('Active requests before paired PR')
info=json.loads(get('/server_info'))
expected=(600000,16,9600000) if a.model=='luz' else (800000,10,8000000)
assert (info['context_length'],info['max_running_requests'],info['max_total_num_tokens'])==expected
(a.output/'server-info.json').write_text(json.dumps(info,indent=2))
rows=[]
for item in suite['rows']:
    assert hashlib.sha256(item['prompt'].encode()).hexdigest()==item['prompt_sha256']
    idle()
    warmup=client.stream(item['warmup_prompt'],8)
    before=idle()
    result=client.stream(item['prompt'],8)
    after=idle()
    ttft=result['t_first']-result['t_req']
    row={'model':a.model,'target':item['target'],'wave':item['wave'],
         'prompt_sha256':item['prompt_sha256'],'prompt_chars':len(item['prompt']),
         'warmup_sha256':item['warmup_sha256'],
         'actual_prompt_tokens':result['prompt_tokens'],'completion_tokens':result['completion'],
         'ttft_s':ttft,'prefill_tps':result['prompt_tokens']/ttft,'warmup':warmup}
    rows.append(row)
    (a.output/'results.json').write_text(json.dumps(rows,indent=2))
    label='w%d-%d'%(item['wave'],item['target'])
    (a.output/(label+'-metrics-before.txt')).write_text(before)
    (a.output/(label+'-metrics-after.txt')).write_text(after)
    print(json.dumps({k:v for k,v in row.items() if k!='warmup'}),flush=True)
(a.output/'complete.json').write_text(json.dumps({'passed':True,'requests':len(rows),
    'model':a.model,'suite_sha256':digest(a.prompts),'client_sha256':digest(pathlib.Path(__file__)),
    'instrument_sha256':digest(a.instrument),'scope':suite['scope']}))
