#!/usr/bin/env python3
"""Install the pinned official Codex native CLI from its npm distribution."""
import base64
import hashlib
import io
import json
from pathlib import Path
import platform
import subprocess
import tarfile
import urllib.request
VERSION='0.147.0'
ROOT=Path(__file__).resolve().parents[1]

def main():
    if platform.system()!='Linux':raise SystemExit('The evaluation runtime requires Linux')
    arch={'x86_64':'x64','aarch64':'arm64'}.get(platform.machine())
    if not arch:raise SystemExit('Unsupported architecture')
    package='@openai/codex'
    distribution_version=VERSION+'-linux-'+arch
    url='https://registry.npmjs.org/'+package.replace('/','%2f')+'/'+distribution_version
    with urllib.request.urlopen(url,timeout=60) as response:metadata=json.load(response)
    assert metadata['name']==package and metadata['version']==distribution_version
    dest=ROOT/'.tools/codex'/VERSION;binary=dest/'codex'
    if binary.exists():
        version=subprocess.check_output([str(binary),'--version'],text=True).strip()
        if not version=='codex-cli '+VERSION:raise ValueError('Existing CLI has a different version')
        print(binary);return
    tarball=metadata['dist']['tarball']
    if not tarball.startswith('https://registry.npmjs.org/'):raise ValueError('Unexpected distribution host')
    with urllib.request.urlopen(tarball,timeout=120) as response:payload=response.read()
    algorithm,expected=metadata['dist']['integrity'].split('-',1)
    if algorithm!='sha512' or hashlib.sha512(payload).digest()!=base64.b64decode(expected):raise ValueError('Official package integrity mismatch')
    with tarfile.open(fileobj=io.BytesIO(payload),mode='r:gz') as archive:
        matches=[m for m in archive.getmembers() if m.isfile() and Path(m.name).name=='codex']
        if len(matches)!=1:raise ValueError('Official package must contain exactly one native CLI')
        data=archive.extractfile(matches[0]).read()
        dest.mkdir(parents=True,exist_ok=True)
        temp=dest/'codex.download';temp.write_bytes(data);temp.chmod(0o755)
        version=subprocess.check_output([str(temp),'--version'],text=True).strip()
        if not version=='codex-cli '+VERSION:raise ValueError('Downloaded CLI version mismatch')
        temp.replace(binary)
    (dest/'distribution.json').write_text(json.dumps({'package':package,'version':distribution_version,'tarball':tarball,'integrity':metadata['dist']['integrity'],'binary_sha256':hashlib.sha256(data).hexdigest()},indent=2)+'\n')
    print(binary)
if __name__=='__main__':main()
