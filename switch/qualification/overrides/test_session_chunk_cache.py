"""CPU dispatch contracts against the image's actual streaming method."""
import ast,os,unittest
from types import SimpleNamespace as NS
import argparse,pathlib,json
p=argparse.ArgumentParser();p.add_argument('--streaming-session-source',required=True,type=pathlib.Path);p.add_argument('--receipt',type=pathlib.Path);args=p.parse_args()
def save(name,value):
 if args.receipt:args.receipt.write_text(json.dumps(value,indent=2)+'\n',encoding='utf-8')
import session_chunk_cache as fix
text=args.streaming_session_source.read_text(encoding='utf-8');tree=ast.parse(text)
cls=next(x for x in tree.body if isinstance(x,ast.ClassDef) and x.name=='StreamingSession')
method=next(x for x in cls.body if isinstance(x,ast.FunctionDef) and x.name=='try_cache_unfinished_req')
src=ast.get_source_segment(text,method)
class Tensor:
 def to(self,**kwargs):return 'prefix-snapshot'
class Matrix:
 def __getitem__(self,key):return Tensor()
class Tests(unittest.TestCase):
 def case(self,enabled=True,streaming=True,slot=None):
  namespace={'torch':NS(int64='int64'),'_is_streaming':lambda r:r.session is not None and r.session.streaming}
  exec('from __future__ import annotations\nclass Session:\n    '+src.replace('\n','\n    '),namespace)
  C=namespace['Session'];os.environ['LUZ_STREAMING_FIRST_CHUNK_CACHE_FIX']='1' if enabled else '0';fix.install(session_cls=C);fix.install(session_cls=C)
  s=C();s.slots={} if slot is None else {'S':slot};s.req_to_token_pool=NS(req_to_token=Matrix())
  r=NS(session=NS(session_id='S',streaming=streaming),kv=NS(req_pool_idx=1),extend_range=NS(end=131072))
  return s,r
 def test_original_shortcuts_first_chunk(self):
  s,r=self.case(enabled=False);self.assertTrue(s.try_cache_unfinished_req(r,chunked=True));self.assertEqual(r.prefix_indices,'prefix-snapshot')
 def test_first_chunk_delegates_to_native_cache(self):
  s,r=self.case();self.assertFalse(s.try_cache_unfinished_req(r,chunked=True));self.assertFalse(hasattr(r,'prefix_indices'))
 def test_released_slot_delegates(self):
  s,r=self.case(slot=NS(kv=NS(holds_kv=False)));self.assertFalse(s.try_cache_unfinished_req(r,chunked=True))
 def test_existing_slot_continuation_unchanged(self):
  s,r=self.case(slot=NS(kv=NS(holds_kv=True)));self.assertTrue(s.try_cache_unfinished_req(r,chunked=True));self.assertEqual(r.prefix_indices,'prefix-snapshot')
 def test_normal_request_unchanged(self):
  s,r=self.case(streaming=False);self.assertFalse(s.try_cache_unfinished_req(r,chunked=True))
 def test_unchunked_first_turn_unchanged(self):
  s,r=self.case();self.assertFalse(s.try_cache_unfinished_req(r,chunked=False))
 def test_unchunked_continuation_unchanged(self):
  s,r=self.case(slot=NS(kv=NS(holds_kv=True)));self.assertTrue(s.try_cache_unfinished_req(r,chunked=False))
if __name__=='__main__':
 suite=unittest.defaultTestLoader.loadTestsFromTestCase(Tests);result=unittest.TextTestRunner(verbosity=2).run(suite)
 save('verification/session-first-chunk-cpu.json',{'passed':result.wasSuccessful(),'tests':result.testsRun,'actual_streaming_source':True,'scope':'Dispatch contracts only; native SWA reclamation and long-context GPU recall still require runtime qualification.'})
 raise SystemExit(not result.wasSuccessful())
