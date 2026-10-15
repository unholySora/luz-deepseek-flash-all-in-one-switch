"""Opt-in first-turn streaming chunk maintenance through the native cache.

The native streaming shortcut bypasses UnifiedRadixCache's SWA maintenance.
For the first turn, while no session slot owns the KV, let the existing cache
handle each unfinished chunk exactly as for a normal chunked request. Slot
creation/finish/restore and subsequent-turn behavior remain native.
"""
import functools,os

def install(module=None,session_cls=None):
    if os.environ.get('LUZ_STREAMING_FIRST_CHUNK_CACHE_FIX')!='1':return
    if session_cls is None:
        from sglang.srt.session.streaming_session import StreamingSession
        session_cls=StreamingSession
    if getattr(session_cls,'_luz_first_chunk_cache_fix',False):return
    original=session_cls.try_cache_unfinished_req
    @functools.wraps(original)
    def wrapped(self,req,chunked=False,**kwargs):
        current=getattr(req,'session',None)
        if chunked and current is not None and current.streaming:
            slot=self.slots.get(current.session_id)
            if slot is None or not slot.kv.holds_kv:
                return False
        return original(self,req,chunked=chunked,**kwargs)
    session_cls.try_cache_unfinished_req=wrapped
    session_cls._luz_first_chunk_cache_fix=True
