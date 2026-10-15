"""Loopback-only tests for preflight, scoped request IDs and failure cleanup."""
import hashlib,http.server,json,pathlib,subprocess,sys,tempfile,threading,unittest

RUNNER=pathlib.Path(__file__).with_name('run_kv_recall.py')

class Transport(unittest.TestCase):
 def exercise(self,enabled=True,fail=False):
  state={'opened':[],'closed':[],'ids':[],'aborts':[]}
  class Handler(http.server.BaseHTTPRequestHandler):
   def log_message(self,*args):pass
   def send(self,value,code=200):
    self.send_response(code);self.send_header('Content-Type','application/json');self.end_headers();self.wfile.write(json.dumps(value).encode())
   def do_GET(self):
    if self.path=='/server_info':self.send({'context_length':600000,'max_total_num_tokens':9600000,'max_running_requests':16,'enable_streaming_session':enabled})
    else:self.send({})
   def do_POST(self):
    body=json.loads(self.rfile.read(int(self.headers['Content-Length'])))
    if self.path=='/open_session':state['opened'].append(body['session_id']);self.send(body['session_id'])
    elif self.path=='/close_session':state['closed'].append(body['session_id']);self.send({})
    elif self.path=='/abort_request':state['aborts'].append(body);self.send({})
    elif self.path=='/generate':
     state['ids'].append(body.get('rid'));query=body['sampling_params']['max_new_tokens']==512
     if query and fail:self.send({'error':'injected query failure'},503);return
     stream=int(body['session_params']['id'].rsplit('-',1)[1])
     self.send({'text':json.dumps({'slot00':'V'+str(stream)}) if query else 'ACK','meta_info':{'prompt_tokens':3,'finish_reason':{'type':'stop'}}})
  server=http.server.ThreadingHTTPServer(('127.0.0.1',0),Handler);thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
  try:
   with tempfile.TemporaryDirectory(prefix='luz-recall-transport-') as td:
    root=pathlib.Path(td);suite=root/'suite';suite.mkdir();rows=[]
    for i in range(2):
     raw=json.dumps({'stream':i,'text_chars':3,'input_tokens':3,'input_ids':[0,1,2],'query_ids':[3], 'expected':{'slot00':'V'+str(i)}}).encode();name=f'stream{i:02d}.json';(suite/name).write_bytes(raw);rows.append({'file':name,'sha256':hashlib.sha256(raw).hexdigest()})
    (suite/'manifest.json').write_text(json.dumps({'format':'luz-switch-kv-recall-v1','context':600000,'pool':9600000,'planned_prompt_tokens':6,'cases':rows}))
    output=root/'output';p=subprocess.run([sys.executable,str(RUNNER),'--suite',str(suite),'--output',str(output),'--base',f'http://127.0.0.1:{server.server_port}','--query-timeout','2','--fill-timeout','2'],capture_output=True,text=True,timeout=15)
    summary=json.loads((output/'summary.json').read_text()) if (output/'summary.json').exists() else None
    return p,state,summary
  finally:server.shutdown();server.server_close();thread.join(timeout=2)
 def test_disabled_session_rejected_before_open(self):
  p,s,_=self.exercise(enabled=False);self.assertNotEqual(p.returncode,0);self.assertIn('--enable-streaming-session',p.stderr);self.assertEqual(s['opened'],[])
 def test_success_has_unique_owned_ids_and_no_self_certification(self):
  p,s,v=self.exercise();self.assertEqual(p.returncode,0,p.stderr);self.assertEqual(v['records_correct'],2);self.assertFalse(v['full_pool_qualified']);self.assertEqual(set(s['opened']),set(s['closed']));self.assertEqual(len(set(s['ids'])),4);prefix=s['aborts'][0]['rid'];self.assertTrue(all(x.startswith(prefix) for x in s['ids']));self.assertNotIn('abort_all',s['aborts'][0])
 def test_http_error_keeps_detail_and_cleans_owned_sessions(self):
  p,s,v=self.exercise(fail=True);self.assertNotEqual(p.returncode,0);self.assertIn('HTTP 503 /generate',p.stderr);self.assertIn('injected query failure',p.stderr);self.assertIsNone(v);self.assertEqual(set(s['opened']),set(s['closed']));self.assertEqual(len(s['aborts']),1)

if __name__=='__main__':unittest.main()
