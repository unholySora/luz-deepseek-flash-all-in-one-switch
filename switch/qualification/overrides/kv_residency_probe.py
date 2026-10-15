"""Read-only scheduler diagnostics, gated by LUZ_KV_PROBE=1.

Does not allocate/free KV, edit requests, change sampling, or add collectives.
Deep snapshots copy page maps on explicit file commands between API phases.
"""
import functools,hashlib,json,os,pathlib,time,traceback

ROOT=pathlib.Path(os.environ.get('LUZ_KV_PROBE_DIR','/state/kv-recall-20261015/probe'))

def _write(path,value):
    path.parent.mkdir(parents=True,exist_ok=True)
    temp=path.with_suffix('.tmp');temp.write_text(json.dumps(value,separators=(',',':'))+'\n');temp.replace(path)

def _integer(value):
    return value if isinstance(value,(int,float,bool,str)) or value is None else str(type(value).__name__)

def _memory():
    mem={x.split(':',1)[0]:int(x.split()[1]) for x in pathlib.Path('/proc/meminfo').read_text().splitlines() if x.startswith(('MemAvailable:','SwapTotal:','SwapFree:'))}
    status={x.split(':',1)[0]:int(x.split()[1]) for x in pathlib.Path('/proc/self/status').read_text().splitlines() if x.startswith(('VmRSS:','VmSwap:'))}
    return {'host':mem,'process':status}

def _session(cache):
    if hasattr(cache,'session') and hasattr(cache.session,'slots'):return cache.session
    if hasattr(cache,'slots'):return cache
    if hasattr(cache,'inner'):return _session(cache.inner)
    raise RuntimeError('Streaming session slot owner not found')

def _snapshot(scheduler,command=None):
    import torch
    cache=scheduler.tree_cache;session=_session(cache)
    allocator=scheduler.token_to_kv_pool_allocator;reqpool=scheduler.req_to_token_pool
    rank=int(os.environ['NODE_RANK']);page_size=int(allocator.page_size)
    result={'epoch':time.time(),'monotonic':time.monotonic(),'rank':rank,'pid':os.getpid(),
            'cache_type':type(cache).__name__,'allocator_type':type(allocator).__name__,
            'page_size':page_size,'max_total_tokens':int(scheduler.max_total_num_tokens),
            'req_pool_size':int(reqpool.size),'req_pool_free':int(reqpool.available_size()),
            'memory':_memory(),'cuda_allocated':torch.cuda.memory_allocated(),'cuda_reserved':torch.cuda.memory_reserved(),
            'cuda_peak_allocated':torch.cuda.max_memory_allocated(),'cuda_peak_reserved':torch.cuda.max_memory_reserved(),
            'peak_reset':getattr(scheduler,'_luz_kv_peak_reset',None),
            'running_req_count':len(getattr(getattr(scheduler,'running_batch',None),'reqs',[])),
            'waiting_req_count':len(getattr(scheduler,'waiting_queue',[])),
            'peak_running_req_count':getattr(scheduler,'_luz_kv_running_peak',0),
            'slots':[],'pools':{}}
    for label,alloc in [('root',allocator),('full',getattr(allocator,'full_attn_allocator',None)),('swa',getattr(allocator,'swa_attn_allocator',None))]:
        if alloc is None:continue
        row={'type':type(alloc).__name__}
        for key in ['size','page_size','_size_full','_size_swa','num_virtual_ids','max_slots']:
            if hasattr(alloc,key):row[key]=_integer(getattr(alloc,key))
        for key in ['available_size','full_available_size','swa_available_size']:
            if hasattr(alloc,key):row[key]=int(getattr(alloc,key)())
        for key in ['free_pages','release_pages','free_virtual_ids']:
            value=getattr(alloc,key,None)
            if value is not None:row[key+'_count']=int(value.numel()) if hasattr(value,'numel') else len(value)
        result['pools'][label]=row
    for name in ['full_evictable_size','full_protected_size','swa_evictable_size','swa_protected_size','session_held_full_tokens','session_held_swa_tokens','session_held_req_count']:
        if hasattr(cache,name):result[name]=int(getattr(cache,name)())
    for sid,slot in sorted(session.slots.items()):
        kv=slot.kv
        result['slots'].append({'id':sid,'row':kv.req_pool_idx,'committed':int(kv.kv_committed_len),
                                'allocated':int(kv.kv_allocated_len),'cache_protected':int(kv.cache_protected_len),
                                'holds_kv':bool(kv.holds_kv),'max_evicted_seqlen':int(kv.max_evicted_seqlen)})
    result['session_count']=len(scheduler.session_controller.sessions)
    if not command:return result
    result['command']=command['id'];result['deep']=bool(command.get('deep'))
    if not command.get('deep'):return result
    import numpy as np
    import re
    assert re.fullmatch(r'[A-Za-z0-9_.-]+',command['id'])
    arrays={};owners={};collisions=[];free_overlaps=[]
    full_alloc=getattr(allocator,'full_attn_allocator',allocator)
    free=[]
    for key in ['free_pages','release_pages','free_virtual_ids']:
        value=getattr(full_alloc,key,None)
        if value is None:continue
        a=value.detach().cpu().numpy() if isinstance(value,torch.Tensor) else np.asarray(value,dtype=np.int64)
        arrays['allocator_'+key]=a;free.append(a)
    free_ids=np.unique(np.concatenate(free)) if free else np.array([],dtype=np.int64)
    cache_object=allocator.get_kvcache()
    result['kvcache_type']=type(cache_object).__name__
    result['kv_components']=[]
    components=[]
    for group in ['kv_pools','index_pools']:
        for ratio,pool in getattr(cache_object,group,{}).items():
            if pool is None:continue
            buffers=[]
            for name,value in vars(pool).items():
                if isinstance(value,torch.Tensor):buffers.append((name,value))
                elif isinstance(value,list) and value and isinstance(value[0],torch.Tensor):buffers.append((name+'[0]',value[0]))
            result['kv_components'].append({'group':group,'ratio':int(ratio),'type':type(pool).__name__,
                'page_size':int(pool.page_size),'size':int(pool.size),
                'buffers':[{'name':name,'shape':list(t.shape),'dtype':str(t.dtype),'bytes':t.numel()*t.element_size()} for name,t in buffers]})
            components.append((group,int(ratio),pool,buffers))
    for index,row in enumerate(result['slots']):
        if not row['holds_kv']:continue
        n=row['committed'];mapping=reqpool.req_to_token[row['row'],:n].detach().cpu().numpy().copy()
        pages=np.unique(mapping.astype(np.int64)//page_size)
        aligned=bool(np.all(mapping%page_size==np.arange(n,dtype=np.int64)%page_size))
        page_sequence=mapping[::page_size].astype(np.int64)//page_size
        arrays['slot%d_pages'%index]=page_sequence
        if not aligned:arrays['slot%d_full_mapping'%index]=mapping
        row.update(mapping_sha256=hashlib.sha256(mapping.tobytes()).hexdigest(),mapping_dtype=str(mapping.dtype),
                   token_min=int(mapping.min()) if n else None,token_max=int(mapping.max()) if n else None,
                   aligned_offsets=aligned,unique_pages=int(len(pages)),page_count_expected=(n+page_size-1)//page_size,
                   mapping_array='slot%d_pages'%index,duplicate_pages_within_slot=int(len(page_sequence)-len(pages)))
        overlap=np.intersect1d(pages,free_ids)
        if overlap.size:free_overlaps.append({'id':row['id'],'pages':overlap[:16].tolist(),'count':len(overlap)})
        for page in pages.tolist():
            if page in owners:collisions.append({'page':page,'a':owners[page],'b':row['id']})
            else:owners[page]=row['id']
        row['samples']=[]
        positions=command.get('positions',{}).get(row['id']) or [int(n*f) for f in [0.01,0.1,0.25,0.5,0.75,0.9,0.99]]
        for requested_position in positions:
            if not n:continue
            position=min(n-1,int(requested_position));location=int(mapping[position])
            sample={'position':position,'logical_location':location,'components':[]}
            for group,ratio,pool,buffers in components:
                page=(location//ratio)//int(pool.page_size)
                eligible=[(name,t) for name,t in buffers if t.ndim>=2 and t.shape[0]>page and any(k in name for k in ['kv_buffer','index_k','k_buffer'])]
                if not eligible:continue
                name,t=eligible[0]
                data=t[page].reshape(-1).view(torch.uint8)[:1024].detach().cpu().numpy().tobytes()
                sample['components'].append({'group':group,'ratio':ratio,'buffer':name,'page':page,
                     'sample_bytes':len(data),'nonzero_bytes':sum(v!=0 for v in data),'sha256':hashlib.sha256(data).hexdigest()})
            row['samples'].append(sample)
    result['cross_slot_page_collisions']=collisions[:64];result['cross_slot_page_collision_count']=len(collisions)
    result['held_pages_in_free_list']=free_overlaps
    result['unique_held_pages']=len(owners)
    artifact=ROOT/(command['id']+'-rank%d-maps.npz'%rank)
    np.savez_compressed(artifact,**arrays);result['mapping_archive']=artifact.name
    result['mapping_archive_sha256']=hashlib.sha256(artifact.read_bytes()).hexdigest()
    result['logical_non_evictable_mapping_verified']=not collisions and not free_overlaps and all(r['aligned_offsets'] and r['duplicate_pages_within_slot']==0 and r['token_min']>0 for r in result['slots'] if r['holds_kv'])
    result['scope']='Committed FULL logical slot ownership plus backing-buffer samples. SWA window retirement is expected. CUDA allocator bytes are not per-session physical DRAM residency.'
    try:result['smaps_rollup']=pathlib.Path('/proc/self/smaps_rollup').read_text()
    except OSError:pass
    return result

def install(module):
    if os.environ.get('LUZ_KV_PROBE')!='1':return
    cls=module.Scheduler
    if getattr(cls,'_luz_kv_probe_installed',False):return
    def event(value):
        ROOT.mkdir(parents=True,exist_ok=True)
        value.update(epoch=time.time(),rank=int(os.environ['NODE_RANK']))
        with (ROOT/('events-rank%s.jsonl'%os.environ['NODE_RANK'])).open('a') as stream:stream.write(json.dumps(value)+'\n')
    from sglang.srt.session.streaming_session import StreamingSession
    match=StreamingSession.try_match_prefix
    @functools.wraps(match)
    def observed_match(self,params):
        result=match(self,params)
        req=params.req
        if req is not None and req.session is not None and req.session.streaming:
            seen=getattr(self,'_luz_probe_seen',set())
            if req.rid not in seen:
                event({'kind':'session_match','session':req.session.session_id,'rid':req.rid,
                       'input_tokens':len(req.origin_input_ids),'matched_tokens':int(result.device_indices.numel()) if result is not None else 0})
                seen.add(req.rid);self._luz_probe_seen=seen
        return result
    StreamingSession.try_match_prefix=observed_match
    release=StreamingSession.release_session
    @functools.wraps(release)
    def observed_release(self,session_id):
        event({'kind':'session_release','session':session_id})
        return release(self,session_id)
    StreamingSession.release_session=observed_release
    from sglang.srt.managers.schedule_batch import ScheduleBatch
    retract=ScheduleBatch.retract_decode
    @functools.wraps(retract)
    def observed_retract(self,*args,**kwargs):
        event({'kind':'retract_decode','rids':[r.rid for r in self.reqs]})
        return retract(self,*args,**kwargs)
    ScheduleBatch.retract_decode=observed_retract
    original=cls.process_input_requests
    @functools.wraps(original)
    def wrapped(self,*args,**kwargs):
        result=original(self,*args,**kwargs)
        self._luz_kv_running_peak=max(getattr(self,'_luz_kv_running_peak',0),len(getattr(getattr(self,'running_batch',None),'reqs',[])))
        current=time.monotonic()
        if current-getattr(self,'_luz_kv_probe_last',0)<1:return result
        self._luz_kv_probe_last=current
        try:
            row=_snapshot(self);rank=int(os.environ['NODE_RANK'])
            _write(ROOT/('latest-rank%d.json'%rank),row)
            command_path=ROOT/'request.json'
            if command_path.exists():
                command=json.loads(command_path.read_text())
                if command['id']!=getattr(self,'_luz_kv_probe_command',None):
                    deep=_snapshot(self,command)
                    if command.get('reset_peak_after'):
                        import torch
                        torch.cuda.reset_peak_memory_stats()
                        self._luz_kv_running_peak=0
                        self._luz_kv_peak_reset={'command':command['id'],'epoch':time.time()}
                        deep['peak_reset_after_snapshot']=self._luz_kv_peak_reset
                    _write(ROOT/(command['id']+'-rank%d.json'%rank),deep)
                    self._luz_kv_probe_command=command['id']
        except Exception:
            _write(ROOT/('error-rank%s.json'%os.environ.get('NODE_RANK','unknown')),{'epoch':time.time(),'traceback':traceback.format_exc()})
        return result
    cls.process_input_requests=wrapped;cls._luz_kv_probe_installed=True
    _write(ROOT/('installed-rank%s-%s.json'%(os.environ.get('NODE_RANK','unknown'),os.getpid())),{'installed':True,'epoch':time.time(),'source_sha256':hashlib.sha256(pathlib.Path(__file__).read_bytes()).hexdigest()})
