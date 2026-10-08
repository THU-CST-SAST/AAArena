"""Export an immutable x86-64 Linux system toolchain, without host credentials."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import platform
import shutil

p = argparse.ArgumentParser()
p.add_argument('--output', type=Path, required=True)
a = p.parse_args()
assert platform.system() == 'Linux' and platform.machine() == 'x86_64'
root = a.output.resolve()
if root.exists():
    raise SystemExit('Use a new empty destination for each runtime snapshot')
root.mkdir(parents=True)
paths = [Path(x) for x in ('/usr/bin','/usr/lib','/usr/lib64','/usr/libexec','/usr/include')]
for pattern in ('cmake*','gcc*','pkgconfig','aclocal*','zoneinfo'):
    paths.extend(sorted(Path('/usr/share').glob(pattern)))
for source in paths:
    if source.exists():
        destination = root / source.relative_to('/')
        destination.parent.mkdir(parents=True, exist_ok=True)
        if source.is_dir() and not source.is_symlink():
            shutil.copytree(source, destination, symlinks=True)
        else:
            shutil.copy2(source, destination, follow_symlinks=False)
for name in ('bin','lib','lib64'):
    source = Path('/') / name
    if source.is_symlink():
        (root / name).symlink_to(os.readlink(source))
    elif source.exists():
        shutil.copytree(source, root / name, symlinks=True)
alternatives = root / 'etc/alternatives'
alternatives.mkdir(parents=True)
for source in Path('/etc/alternatives').iterdir():
    if source.is_symlink() and os.readlink(source).startswith('/usr/'):
        (alternatives / source.name).symlink_to(os.readlink(source))
(root/'etc/passwd').write_text('root:x:0:0:root:/tmp:/bin/sh\nnobody:x:65534:65534:nobody:/tmp:/bin/sh\n')
(root/'etc/group').write_text('root:x:0:\nnogroup:x:65534:\n')
(root/'etc/nsswitch.conf').write_text('passwd: files\ngroup: files\nhosts: files\n')
for name in ('os-release','localtime','timezone','ld.so.cache'):
    source = Path('/etc') / name
    if source.is_file():
        shutil.copy2(source, root/'etc'/name)
files = []
for path in sorted(root.rglob('*')):
    if path.is_symlink():
        files.append({'path':str(path.relative_to(root)), 'symlink':os.readlink(path)})
    elif path.is_file():
        with path.open('rb') as f:
            digest = hashlib.file_digest(f, 'sha256').hexdigest()
        files.append({'path':str(path.relative_to(root)), 'size':path.stat().st_size,'sha256':digest})
manifest = json.dumps(files,sort_keys=True,separators=(',',':')).encode()
(root/'runtime-files.json').write_bytes(manifest)
(root/'aa-arena-runtime.json').write_text(json.dumps({
    'schema_version':1,'architecture':'x86_64',
    'files_sha256':hashlib.sha256(manifest).hexdigest(),
    'file_count':len(files),
    'distribution':(root/'etc/os-release').read_text(),
},indent=2)+'\n')
print(json.dumps({'root':str(root),'files':len(files),'manifest_sha256':hashlib.sha256(manifest).hexdigest()}))
