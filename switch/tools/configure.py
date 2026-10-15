"""Render topology-specific manifests without changing algorithm settings."""
import argparse,hashlib,json,pathlib,shutil
p=argparse.ArgumentParser()
p.add_argument('--cluster',required=True,type=pathlib.Path)
p.add_argument('--profile',choices=['b20-throughput','b32-memory'],default='b20-throughput')
p.add_argument('--output',required=True,type=pathlib.Path)
a=p.parse_args();B=pathlib.Path(__file__).resolve().parents[1]
c=json.loads(a.cluster.read_text());nodes=sorted(c['nodes'],key=lambda n:n['rank'])
assert [n['rank'] for n in nodes]==list(range(4))
assert c['image_id'].startswith('sha256:') and len(c['image_id'])==71
assert len({n['name'] for n in nodes})==4
for n in nodes:
 assert pathlib.PurePosixPath(n['root']).is_absolute() and pathlib.PurePosixPath(n['model']).is_absolute()
 assert all(len(n[k])==2 for k in ['hcas','netdevs','rail_ips'])
 assert n['name'].replace('-','').replace('_','').isalnum()
out=a.output.resolve();assert not out.exists(),'Use a new output directory'
out.mkdir(parents=True)
manifest=[]
for n in nodes:
 rank=n['rank'];raw=(B/'profiles'/a.profile/('rank%d.json'%rank)).read_text()
 mapping={'ROOT':n['root'],'MODEL':n['model'],'HEAD_IP':nodes[0]['rail_ips'][0],
  'RAIL0_IP':n['rail_ips'][0],'RAIL1_IP':n['rail_ips'][1],
  'HCA0':n['hcas'][0],'HCA1':n['hcas'][1],'NETDEV0':n['netdevs'][0],'NETDEV1':n['netdevs'][1]}
 for key,value in mapping.items():raw=raw.replace('${'+key+'}',value)
 assert '${' not in raw
 spec=json.loads(raw);spec['image']=c['image'];spec['image_id']=c['image_id'];spec['port']=c['port']
 spec['rails']={n['hcas'][i]:n['rail_ips'][i] for i in range(2)}
 spec['args'][spec['args'].index('--port')+1]=str(c['port'])
 dest=out/n['name'];dest.mkdir()
 (dest/'runtime.json').write_text(json.dumps(spec,indent=2)+'\n',encoding='utf-8')
 for name in ['node.py','control.py']:shutil.copyfile(B/'tools'/name,dest/name)
 shutil.copytree(B/'overrides',dest/'overrides')
 cluster={'url':'http://127.0.0.1:'+str(c['port']),'nodes':[{'rank':m['rank'],'name':m['name'],'ssh':m['ssh'],'path':m['root'],'sudo':bool(m.get('sudo',False))} for m in nodes]}
 (dest/'cluster.json').write_text(json.dumps(cluster,indent=2)+'\n',encoding='utf-8')
 manifest.append({'node':n['name'],'rank':rank,'runtime_sha256':hashlib.sha256((dest/'runtime.json').read_bytes()).hexdigest()})
(out/'manifest.json').write_text(json.dumps({'profile':a.profile,'nodes':manifest},indent=2)+'\n',encoding='utf-8')
print(json.dumps({'generated':str(out),'profile':a.profile,'nodes':manifest}))
