"""Regrade synthetic responses and reconstruct all published FULL page maps.

Requires numpy. No network, GPU, serving or configuration changes.
"""
import argparse,hashlib,json,pathlib
import numpy as np
from run_kv_recall import grade

def main():
 p=argparse.ArgumentParser(description=__doc__);p.add_argument('--evidence',required=True,type=pathlib.Path);p.add_argument('--receipt',type=pathlib.Path);a=p.parse_args()
 def read(path):return json.loads(path.read_text(encoding='utf-8'))
 result=read(a.evidence/'results.json');stages={s['job']:s for s in result['stages']};history={};files=0;maps_count=0
 for path in sorted((a.evidence/'pages').glob('*/*/rank*.json')):
  d=read(path);archive=path.with_name(d['mapping_archive']);assert hashlib.sha256(archive.read_bytes()).hexdigest()==d['mapping_archive_sha256']
  stage=stages[d['stage']];slots=d['slots'];wanted=0 if d['phase'] in ['before','closed'] else stage['concurrency'];assert len(slots)==wanted
  assert d['logical_non_evictable_mapping_verified'] and not d['cross_slot_page_collision_count'] and not d['held_pages_in_free_list']
  stored={};samples={}
  with np.load(archive,allow_pickle=False) as z:
   free=np.concatenate([z[k].reshape(-1) for k in z.files if k.startswith('allocator_')]);pages=[]
   for s in slots:
    assert s['holds_kv'] and s['aligned_offsets'];n=s['committed'];page_size=d['page_size'];seq=z[s['mapping_array']].astype(np.int64)
    assert len(seq)==(n+page_size-1)//page_size
    rebuilt=(np.repeat(seq,page_size)[:n]*page_size+np.arange(n,dtype=np.int64)%page_size).astype(s['mapping_dtype'])
    assert hashlib.sha256(rebuilt.tobytes()).hexdigest()==s['mapping_sha256']
    assert len(np.unique(seq))==len(seq) and not np.intersect1d(seq,free).size
    assert len(s['samples'])==7 and all(v['components'] and all(c['sample_bytes']>0 and c['nonzero_bytes']>0 for c in v['components']) for v in s['samples'])
    stored[s['id']]=seq.copy();samples[s['id']]=s['samples'];pages.append(seq);maps_count+=1
   if pages:
    all_pages=np.concatenate(pages);assert len(np.unique(all_pages))==len(all_pages)
  if d['phase']=='closed':assert d['req_pool_free']==d['req_pool_size'] and d['full_protected_size']==0 and d['swa_protected_size']==0
  history[(d['stage'],d['phase'],d['rank'])]={'pages':stored,'samples':samples,'digests':{s['id']:s['mapping_sha256'] for s in slots}}
  files+=1
 for job in stages:
  for phase in ['plateau','after-query']:
   baseline=history[(job,phase,0)]['digests']
   for rank in range(1,4):assert history[(job,phase,rank)]['digests']==baseline
  for rank in range(4):
   before=history[(job,'plateau',rank)];after=history[(job,'after-query',rank)]
   assert before['samples']==after['samples']
   for sid,seq in before['pages'].items():assert np.array_equal(seq[:-1],after['pages'][sid][:len(seq)-1])
 count=0;correct=0
 for item in read(a.evidence/'recall-evidence.json'):
  values={}
  for r in item['records']:
   for k,v in r['expected'].items():values.setdefault(k,set()).add(v)
  scores=[]
  for r in item['records']:
   other={k:v-{r['expected'][k]} for k,v in values.items()};s=grade(r['expected'],r['query_text'],other);scores.append(s);count+=s['total'];correct+=s['correct']
   original=next(s for s in stages[item['job']]['streams'] if s['stream']==r['stream']);assert s==original['grade']
   fm=r['fill_meta_info'];qm=r['meta_info'];assert fm['prompt_tokens']==r['input_tokens']==original['input_tokens']
   assert r['fill_text'].strip()=='ACK' and fm['finish_reason']['type']=='stop'
   assert (fm.get('cached_tokens') or 0)==original['fill_cached_tokens'] and (fm.get('num_retractions') or 0)==original['fill_retractions']
   cached=qm.get('cached_tokens_details') or {};assert cached.get('device',0)==original['query_cached_device_tokens'] and cached.get('host',0)==original['query_cached_host_tokens']
   assert qm['finish_reason']['type']==original['query_finish'] and (qm.get('num_retractions') or 0)==original['query_retractions']
  assert sum(s['correct'] for s in scores)==stages[item['job']]['correct']
 receipt={'passed':True,'snapshots_verified':files,'slot_maps_reconstructed':maps_count,'recall_correct':correct,'recall_total':count,
  'scope':'Archived logical maps and response scoring. Does not re-run GPU inference or inspect Linux physical-page residency.'}
 trace_index=a.evidence/'memory-traces.json'
 if trace_index.exists():
  traces={}
  for item in read(trace_index):
   raw=(a.evidence/item['file']).read_bytes();assert hashlib.sha256(raw).hexdigest()==item['sha256'];traces[(item.get('series'),item['rank'])]=[json.loads(x) for x in raw.splitlines()]
  windows=0
  for stage in stages.values():
   for rank,m in stage['memory'].items():
    selected=[r for r in traces[(stage.get('resource_series'),rank)] if stage['start_epoch']-2<=r['epoch']<=stage['end_epoch']+2];assert selected
    assert min(r['available_gib'] for r in selected)==m['min_available_gib']
    assert max(r['cgroup_swap_bytes'] for r in selected)==m['max_container_swap_bytes']
    assert 4*(selected[-1]['vmstat']['pswpout']-selected[0]['vmstat']['pswpout'])==m['host_swapout_KiB'];windows+=1
    assert 4*(selected[-1]['vmstat']['pswpin']-selected[0]['vmstat']['pswpin'])==m['host_swapin_KiB']
    for event,key in [('oom','cgroup_oom_delta'),('oom_kill','cgroup_oom_kill_delta'),('max','cgroup_max_delta')]:
     assert selected[-1]['memory_events'].get(event,0)-selected[0]['memory_events'].get(event,0)==m[key]
    gaps=np.diff([r['epoch'] for r in selected])
    if len(gaps) and 'max_sample_gap_s' in m:
     assert float(max(gaps))==m['max_sample_gap_s'] and float(np.median(gaps))==m['median_sample_gap_s']
  receipt['memory_windows_verified']=windows
 for stage in stages.values():
  if not stage['full_pool_qualified']:continue
  assert stage['kind']=='capacity-ladder' and stage['concurrency']==16
  assert stage['input_tokens']>=.99*result['actual_pool']
  assert stage['correct']==stage['total']==112 and stage['wrong_stream']==0 and stage['format_errors']==0
  assert len(stage['memory'])==4 and all(m['min_available_gib']>=4 and m['max_container_swap_bytes']==0 and m['cgroup_oom_delta']==0 and m['cgroup_oom_kill_delta']==0 for m in stage['memory'].values())
  assert all(s['fill_cached_tokens']==0 and s['query_cached_device_tokens']>=s['input_tokens']-512 and s['query_cached_host_tokens']==0 and s['fill_retractions']==0 and s['query_retractions']==0 and s['query_finish']=='stop' for s in stage['streams'])
 if a.receipt:a.receipt.write_text(json.dumps(receipt,indent=2)+'\n',encoding='utf-8')
 print(json.dumps(receipt))

if __name__=='__main__':main()
