"""Distribution hygiene inside an image build; no inference code changes."""
import pathlib
import re

source = pathlib.Path('/opt/dsv41/adapter/lm_cert.py')
text = source.read_text()
changed, count = re.subn(r'192[.]168[.]\d+[.]\d+:5000/lmsysorg/sglang',
                          'upstream-registry.example/lmsysorg/sglang', text)
if count not in [0, 1]:
    raise ValueError('Unexpected upstream documentation layout')
if count:
    source.write_text(changed)
for algorithm in ['rsa', 'ecdsa', 'ed25519']:
    for suffix in ['', '.pub']:
        pathlib.Path(f'/etc/ssh/ssh_host_{algorithm}_key{suffix}').unlink(missing_ok=True)
# A build-layer deletion does not erase bytes in inherited base layers. See
# image-delta/README.zh-CN.md for the exact distribution and scan boundaries.
