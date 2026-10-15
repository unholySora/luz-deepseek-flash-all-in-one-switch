"""CPU admission contracts, including the image's exact row-reuse allocator."""
import ast,json,os,pathlib,types,unittest
from types import SimpleNamespace as NS
import argparse,pathlib,json
p=argparse.ArgumentParser();p.add_argument('--memory-pool-source',required=True,type=pathlib.Path);p.add_argument('--receipt',type=pathlib.Path);args=p.parse_args()
def save(name,value):
 if args.receipt:args.receipt.write_text(json.dumps(value,indent=2)+'\n',encoding='utf-8')
import session_row_admission as fix

text=args.memory_pool_source.read_text(encoding='utf-8')
tree=ast.parse(text);node=next(n for n in tree.body if isinstance(n,ast.ClassDef) and n.name=='ReqToTokenPool')
methods=[ast.get_source_segment(text,n) for n in node.body if isinstance(n,ast.FunctionDef) and n.name in ['alloc','alloc_rows']]
namespace={};exec('from __future__ import annotations\nclass RowPool:\n'+'\n'.join('    '+s.replace('\n','\n    ') for s in methods),namespace)
BasePool=namespace['RowPool']
class Pool(BasePool):
 def __init__(self,n=0):self.free_slots=list(range(101,101+n))
 def available_size(self):return len(self.free_slots)

def req(name,row=None,streaming=True,abort=False,beam=False):
 kv=NS(req_pool_idx=row,holds_kv=row is not None,kv_allocated_len=100 if row else 0)
 return NS(rid=name,session=NS(streaming=streaming,session_id=name) if streaming else None,to_finish='abort' if abort else None,beam_group=NS() if beam else None,kv=kv)

class Tests(unittest.TestCase):
 def scheduler(self,requests,slots,free=0,running=0,fail=False,enabled=True):
  module=NS(get_parallel=lambda:NS(pp_max_micro_batch_size=16))
  class Scheduler:
   def get_num_allocatable_reqs(self,running_bs,beam_width=None,running_batch=None):return min(16-running_bs,self.req_to_token_pool.available_size())
   def _get_new_batch_prefill_raw(self,prefill_delayer_single_pass=None,running_batch=None):
    batch=running_batch or self.running_batch
    if self.get_num_allocatable_reqs(len(batch.reqs),running_batch=batch)<1:return []
    if fail:raise RuntimeError('fixture failure')
    admitted=[]
    for r in self.waiting_queue:
     if len(admitted)>=self.get_num_allocatable_reqs(len(batch.reqs),running_batch=batch):break
     if r.session and r.rid in self.tree_cache.session.slots:r.kv=self.tree_cache.session.slots[r.rid].kv
     admitted.append(r)
    rows=self.req_to_token_pool.alloc(admitted);assert rows is not None
    self.waiting_queue=self.waiting_queue[len(admitted):]
    return rows
  module.Scheduler=Scheduler
  os.environ['LUZ_STREAMING_ROW_ADMISSION_FIX']='1' if enabled else '0';fix.install(module);fix.install(module)
  s=Scheduler();s.req_to_token_pool=Pool(free);s.tree_cache=NS(session=NS(slots=slots));s.chunked_req=None
  s.waiting_queue=requests;s.running_batch=NS(reqs=[req('active'+str(i),i+1) for i in range(running)])
  return s
 def slots(self,requests):return {r.rid:NS(kv=NS(req_pool_idx=i+1,holds_kv=True,kv_allocated_len=100)) for i,r in enumerate(requests)}
 def test_baseline_reproduces_deadlock(self):
  q=[req(str(i)) for i in range(16)];s=self.scheduler(q,self.slots(q),enabled=False);self.assertEqual(s._get_new_batch_prefill_raw(),[])
 def test_full_sixteen_reuses_exact_owned_rows(self):
  q=[req(str(i)) for i in range(16)];s=self.scheduler(q,self.slots(q));self.assertEqual(s._get_new_batch_prefill_raw(),list(range(1,17)));self.assertEqual(s.req_to_token_pool.free_slots,[]);self.assertEqual(s.waiting_queue,[])
 def test_fresh_front_request_remains_queued(self):
  q=[req('new',streaming=False),req('held')];s=self.scheduler(q,self.slots(q[1:]));self.assertEqual(s._get_new_batch_prefill_raw(),[1]);self.assertEqual([r.rid for r in s.waiting_queue],['new'])
 def test_invalid_and_beam_are_not_borrowers(self):
  q=[req('aborted',abort=True),req('beam',beam=True),req('missing')];s=self.scheduler(q,self.slots(q[:2]));self.assertEqual(s._get_new_batch_prefill_raw(),[]);self.assertEqual(s.waiting_queue,q)
 def test_active_owned_row_not_reused(self):
  q=[req('same')];s=self.scheduler(q,self.slots(q),running=1);self.assertEqual(s._get_new_batch_prefill_raw(),[])
 def test_released_slot_is_not_credit(self):
  q=[req('gone')];slots=self.slots(q);slots['gone'].kv.holds_kv=False;s=self.scheduler(q,slots);self.assertEqual(s._get_new_batch_prefill_raw(),[])
 def test_duplicate_row_is_not_double_admitted(self):
  q=[req('a'),req('b')];slots=self.slots(q);slots['b'].kv=slots['a'].kv;s=self.scheduler(q,slots);self.assertEqual(s._get_new_batch_prefill_raw(),[1]);self.assertEqual([r.rid for r in s.waiting_queue],['b'])
 def test_running_budget_remains_sixteen(self):
  q=[req('a'),req('b')];slots=self.slots(q);slots['a'].kv.req_pool_idx=31;slots['b'].kv.req_pool_idx=32;s=self.scheduler(q,slots,running=15);self.assertEqual(s._get_new_batch_prefill_raw(),[31]);self.assertEqual([r.rid for r in s.waiting_queue],['b'])
 def test_exception_restores_queue_and_context(self):
  q=[req('new',streaming=False),req('held')];s=self.scheduler(q,self.slots(q[1:]),fail=True)
  with self.assertRaises(RuntimeError):s._get_new_batch_prefill_raw()
  self.assertEqual(s.waiting_queue,q);self.assertFalse(hasattr(s,'_luz_reusable_row_credit'))
 def test_free_row_normal_path_is_unchanged(self):
  q=[req('held')];s=self.scheduler(q,self.slots(q),free=1);self.assertEqual(s._get_new_batch_prefill_raw(),[1]);self.assertEqual(s.req_to_token_pool.free_slots,[101])

if __name__=='__main__':
 suite=unittest.defaultTestLoader.loadTestsFromTestCase(Tests);result=unittest.TextTestRunner(verbosity=2).run(suite)
 save('verification/session-row-admission-cpu.json',{'passed':result.wasSuccessful(),'tests':result.testsRun,'real_allocator_source':'memory_pool.py:ReqToTokenPool.alloc+alloc_rows','numeric_or_gpu_qualification':False})
 raise SystemExit(not result.wasSuccessful())
