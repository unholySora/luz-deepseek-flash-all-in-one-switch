"""Opt-in repair for full-row streaming sessions, separate from diagnostics.

At zero globally free request rows, only continuations with their own valid
held row may enter. Ordinary/beam/pre-aborted requests remain queued. KV data,
sampling, request-row allocation and the normal non-full admission path stay
with SGLang. All wrappers are installed once before scheduler construction.
"""
import functools,os


def _session(cache):
    if hasattr(cache,'session') and hasattr(cache.session,'slots'):return cache.session
    if hasattr(cache,'slots'):return cache
    if hasattr(cache,'inner'):return _session(cache.inner)
    return None


def install(module):
    if os.environ.get('LUZ_STREAMING_ROW_ADMISSION_FIX')!='1':return
    cls=module.Scheduler
    if getattr(cls,'_luz_session_row_admission',False):return
    raw=cls._get_new_batch_prefill_raw
    count=cls.get_num_allocatable_reqs

    @functools.wraps(count)
    def available(self,running_bs,beam_width=None,running_batch=None):
        original=count(self,running_bs,beam_width,running_batch)
        credit=getattr(self,'_luz_reusable_row_credit',0)
        if not credit or beam_width is not None:return original
        budget=max(0,module.get_parallel().pp_max_micro_batch_size-running_bs)
        return max(original,min(budget,credit))

    @functools.wraps(raw)
    def prefill(self,*args,**kwargs):
        if self.req_to_token_pool.available_size()!=0 or self.chunked_req is not None:
            return raw(self,*args,**kwargs)
        session=_session(self.tree_cache)
        if session is None:return raw(self,*args,**kwargs)
        batch=kwargs.get('running_batch')
        if batch is None:batch=args[1] if len(args)>1 else self.running_batch
        active_rows={r.kv.req_pool_idx for r in batch.reqs if r.kv.holds_kv}
        original=list(self.waiting_queue);selected=[];seen=set(active_rows)
        for req in original:
            current=getattr(req,'session',None)
            if current is None or not current.streaming or req.to_finish is not None or req.beam_group is not None:continue
            slot=session.slots.get(current.session_id)
            if slot is None or not slot.kv.holds_kv or slot.kv.kv_allocated_len<=0:continue
            row=slot.kv.req_pool_idx
            if row in seen:continue
            selected.append(req);seen.add(row)
        if not selected:return raw(self,*args,**kwargs)
        chosen={id(r) for r in selected};before={id(r) for r in original}
        self.waiting_queue=selected
        self._luz_reusable_row_credit=len(selected)
        try:
            return raw(self,*args,**kwargs)
        finally:
            remaining=list(self.waiting_queue);remaining_ids={id(r) for r in remaining}
            self.waiting_queue=[r for r in original if id(r) not in chosen or id(r) in remaining_ids]
            self.waiting_queue.extend(r for r in remaining if id(r) not in before)
            del self._luz_reusable_row_credit

    cls.get_num_allocatable_reqs=available
    cls._get_new_batch_prefill_raw=prefill
    cls._luz_session_row_admission=True
