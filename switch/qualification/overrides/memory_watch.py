"""Host/container memory guard, CPU only; no model/config mutations."""
import argparse,json,pathlib,subprocess,time
p=argparse.ArgumentParser();p.add_argument('--root',required=True);p.add_argument('--container',required=True);p.add_argument('--interval',type=float,default=2);a=p.parse_args();assert .2<=a.interval<=5
root=pathlib.Path(a.root);root.mkdir(parents=True,exist_ok=False)
obj=json.loads(subprocess.check_output(['docker','inspect',a.container]))[0];pid=obj['State']['Pid']
cg=pathlib.Path('/sys/fs/cgroup')/pathlib.Path(f'/proc/{pid}/cgroup').read_text().split('::',1)[1].strip().lstrip('/')
def write(name,value):
 q=root/name;t=q.with_suffix('.tmp');t.write_text(json.dumps(value)+'\n');t.replace(q)
rows=[];low=0;started=time.time();initial=None
with (root/'samples.jsonl').open('a',buffering=1) as out:
 while not (root/'stop').exists() and time.time()-started<25200:
  if not pathlib.Path(f'/proc/{pid}').exists():
   write('fatal.json',{'epoch':time.time(),'reason':'container-process-exited'});break
  m={x.split(':',1)[0]:int(x.split()[1]) for x in pathlib.Path('/proc/meminfo').read_text().splitlines() if x.startswith(('MemAvailable:','SwapTotal:','SwapFree:'))}
  vm={x.split()[0]:int(x.split()[1]) for x in pathlib.Path('/proc/vmstat').read_text().splitlines() if x.startswith(('pswpin ','pswpout '))}
  ev={x.split()[0]:int(x.split()[1]) for x in (cg/'memory.events').read_text().splitlines()}
  row={'epoch':time.time(),'available_gib':m['MemAvailable']/1048576,'host_swap_mib':(m['SwapTotal']-m['SwapFree'])/1024,
       'vmstat':vm,'memory_events':ev,'cgroup_swap_bytes':int((cg/'memory.swap.current').read_text()),
       'memory_pressure':pathlib.Path('/proc/pressure/memory').read_text()}
  if initial is None:initial=row
  rows.append(row);out.write(json.dumps(row)+'\n');write('latest.json',row)
  low=low+1 if row['available_gib']<4 else 0
  window=rows[-(int(30/a.interval)+1):];swap_mib=(window[-1]['vmstat']['pswpout']-window[0]['vmstat']['pswpout'])/256
  increases=sum(y['vmstat']['pswpout']>x['vmstat']['pswpout'] for x,y in zip(window,window[1:]))
  reason=None
  if row['memory_events'].get('oom_kill',0)>initial['memory_events'].get('oom_kill',0):reason='oom-kill'
  elif row['memory_events'].get('oom',0)>initial['memory_events'].get('oom',0):reason='cgroup-oom'
  elif row['cgroup_swap_bytes']>0:reason='container-swap'
  elif low>=3:reason='MemAvailable-below-4GiB-for-3-samples'
  elif swap_mib>=64 and increases>=3:reason='sustained-host-swapout-64MiB-window'
  if reason:
   write('fatal.json',{'epoch':time.time(),'reason':reason,'sample':row,'window_swapout_mib':swap_mib});break
  time.sleep(a.interval)
if rows:
 write('summary.json',{'samples':len(rows),'min_available_gib':min(x['available_gib'] for x in rows),
 'max_host_swap_mib':max(x['host_swap_mib'] for x in rows),'max_cgroup_swap_bytes':max(x['cgroup_swap_bytes'] for x in rows),
 'pswpout_delta':rows[-1]['vmstat']['pswpout']-rows[0]['vmstat']['pswpout'],
 'pswpin_delta':rows[-1]['vmstat']['pswpin']-rows[0]['vmstat']['pswpin'],
 'oom_kill_delta':rows[-1]['memory_events'].get('oom_kill',0)-rows[0]['memory_events'].get('oom_kill',0)})
