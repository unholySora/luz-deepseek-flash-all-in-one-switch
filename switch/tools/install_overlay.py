import pathlib,shutil
base=pathlib.Path('/opt/dsv41/overlay')
target=pathlib.Path('/sgl-workspace/sglang/python/sglang')
assert (target/'__init__.py').is_file()
rows=[line.split('\t') for line in (base/'overlay-map.tsv').read_text().splitlines() if line and not line.startswith('#')]
assert len(rows)==28
for source,relative in rows:
 destination=target/relative
 assert destination.resolve().is_relative_to(target.resolve())
 destination.parent.mkdir(parents=True,exist_ok=True)
 shutil.copyfile(base/source,destination)
print('Installed 28 matching SGLang overlay files')
