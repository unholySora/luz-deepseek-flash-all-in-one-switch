import hashlib,json,pathlib,sys,tempfile,types
import torch
sys.path.insert(0,'/probe')
import kv_residency_probe as probe
from types import SimpleNamespace as NS
probe.ROOT=pathlib.Path(tempfile.mkdtemp(prefix='probe-cpu-'))
page_size=256
mapping=torch.zeros((4,1024),dtype=torch.int32)
mapping[1,:512]=torch.arange(256,768,dtype=torch.int32)
mapping[2,:512]=torch.arange(768,1280,dtype=torch.int32)
def kv(row):return NS(req_pool_idx=row,kv_committed_len=512,kv_allocated_len=512,cache_protected_len=0,holds_kv=True,max_evicted_seqlen=0)
slots={'S0':NS(kv=kv(1)),'S1':NS(kv=kv(2))}
pool=NS(size=5120,page_size=256,kv_buffer=[torch.arange(32*2048,dtype=torch.int64).to(torch.uint8).reshape(32,2048)])
cacheobj=NS(kv_pools={1:pool},index_pools={})
alloc=NS(size=5120,page_size=256,free_pages=torch.arange(5,21,dtype=torch.int64),release_pages=torch.empty(0,dtype=torch.int64),available_size=lambda:4096)
alloc.get_kvcache=lambda:cacheobj
cache=NS(session=NS(slots=slots),full_evictable_size=lambda:0,full_protected_size=lambda:0,
         session_held_full_tokens=lambda:1024,session_held_swa_tokens=lambda:0,session_held_req_count=lambda:2)
rp=NS(size=4,available_size=lambda:2,req_to_token=mapping)
s=NS(tree_cache=cache,token_to_kv_pool_allocator=alloc,req_to_token_pool=rp,max_total_num_tokens=5120,session_controller=NS(sessions={'S0':1,'S1':2}))
before=mapping.clone()
r=probe._snapshot(s,{'id':'valid','deep':True});assert r['logical_non_evictable_mapping_verified'] and r['unique_held_pages']==4
assert torch.equal(before,mapping)
mapping[2,:512]=mapping[1,:512]
r=probe._snapshot(s,{'id':'overlap','deep':True});assert not r['logical_non_evictable_mapping_verified'] and r['cross_slot_page_collision_count']==2
mapping.copy_(before);alloc.free_pages=torch.tensor([1,5,6])
r=probe._snapshot(s,{'id':'free-overlap','deep':True});assert not r['logical_non_evictable_mapping_verified'] and r['held_pages_in_free_list']
assert torch.equal(before,mapping)
print(json.dumps({'passed':True,'real_cpu_torch':True,'valid_map_verified':True,'cross_slot_alias_detected':True,'held_free_overlap_detected':True,'input_mappings_not_mutated':True,'gpu_test':False}))
