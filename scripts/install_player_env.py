#!/usr/bin/env python3
"""Install an isolated Python 3.10 player environment using a provided interpreter."""
import argparse
import json
import subprocess
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]

def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--python',default='python3.10')
    p.add_argument('--destination',type=Path,default=ROOT/'.player-env')
    a=p.parse_args();dest=a.destination.resolve()
    version=json.loads(subprocess.check_output([a.python,'-I','-c','import sys,json;print(json.dumps(list(sys.version_info[:3])))'],text=True))
    if version[:2]!=[3,10]:p.error('Players require Python 3.10; the environment specification pins 3.10.14')
    if dest.exists():p.error('Destination already exists; choose a new empty directory')
    subprocess.run([a.python,'-m','venv','--symlinks',str(dest)],check=True)
    python=dest/'bin/python'
    subprocess.run([str(python),'-m','pip','install','pip==24.3.1'],check=True)
    subprocess.run([str(python),'-m','pip','install','-r',str(ROOT/'environment/player-requirements.txt')],check=True)
    record={'python':version,'requirements':'environment/player-requirements.txt'}
    (dest/'arena-install.json').write_text(json.dumps(record,indent=2)+'\n')
    print('Player environment:',dest)
if __name__=='__main__':main()
