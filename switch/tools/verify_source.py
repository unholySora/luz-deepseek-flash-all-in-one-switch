"""Verify LF-normalized build inputs and exact qualified overlay bytes."""
import hashlib,json,pathlib
B=pathlib.Path(__file__).resolve().parents[1]
root=B.parent
data=json.loads((B/'image-context/source-manifest.json').read_text())
for row in data['files']:
 path=B/'image-context/_roce_proxy.c' if row['override'] else root/row['path']
 digest=hashlib.sha256(path.read_bytes().replace(b'\r\n',b'\n')).hexdigest()
 assert digest==row['sha256_lf_normalized'],'Build source mismatch: '+row['path']
overlays=json.loads((B/'overrides/manifest.json').read_text())
for row in overlays:
 assert hashlib.sha256((B/row['path']).read_bytes()).hexdigest()==row['sha256'],'Overlay byte mismatch: '+row['path']
print(json.dumps({'passed':True,'build_source_files':len(data['files']),'qualified_overlays':len(overlays),
 'upstream_base':data['upstream_commit'],'scope':'Source and overlay checks; not a Docker build or serving qualification'}))
