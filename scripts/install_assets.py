#!/usr/bin/env python3
"""Install and verify the bundled game packs without network access."""
from __future__ import annotations
import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import tarfile
import tempfile

ROOT = Path(__file__).resolve().parents[1]

def digest(path):
    with Path(path).open('rb') as f:
        h=hashlib.sha256()
        for block in iter(lambda:f.read(1024*1024),b''):h.update(block)
        return h.hexdigest()

def safe_name(name):
    p=PurePosixPath(name)
    if p.is_absolute() or '..' in p.parts or '\\' in name or not p.parts:
        raise ValueError('Unsafe archive member path')
    return p

def verify_game(root, manifest):
    for entry in manifest['files']:
        relative=safe_name(entry['path']);p=root/relative
        if not p.resolve().is_relative_to(root.resolve()) or p.is_symlink() or not p.is_file():
            raise ValueError(f'Missing or unsafe asset: {relative}')
        if p.stat().st_size!=entry['size'] or digest(p)!=entry['sha256']:
            raise ValueError(f'Asset hash mismatch: {relative}')
    for entry in manifest['files']:
        if entry['path'].endswith('/players/publication.json'):
            publication=root/safe_name(entry['path'])
            expected={row['player_id'] for row in json.loads(publication.read_text())['published_players']}
            pool=publication.parent/'pool'
            actual={path.name for path in pool.iterdir() if path.is_dir()}
            if actual!=expected:
                raise ValueError('Installed pool includes missing or non-public packages; install the public bundle into a fresh directory')
    return len(manifest['files'])

def install_game(root, row, verify_only=False):
    game=row['game'];pack=ROOT/'assets'/row['archive']
    if not pack.resolve().is_relative_to((ROOT/'assets').resolve()):raise ValueError('Invalid archive path')
    if digest(pack)!=row['sha256']:raise ValueError(f'Archive hash mismatch: {game}')
    with tarfile.open(pack,'r:gz') as archive:
        members=archive.getmembers();names=[m.name for m in members]
        if len(names)!=len(set(names)):raise ValueError('Duplicate archive members')
        manifest_name=f'assets/manifests/{game}.json'
        manifest=json.load(archive.extractfile(manifest_name))
        expected={x['path'] for x in manifest['files']}|{manifest_name}
        if set(names)!=expected:raise ValueError('Archive does not match file manifest')
        for member in members:
            path=safe_name(member.name)
            if not member.isfile() or (member.name!=manifest_name and path.parts[:2]!=('games',game)):
                raise ValueError('Unsupported asset member')
        if verify_only or (root/'games'/game).exists():
            count=verify_game(root,manifest)
            print(f'{game}: verified {count} files',flush=True)
            return
        root.mkdir(parents=True,exist_ok=True)
        with tempfile.TemporaryDirectory(prefix='.arena-assets-',dir=root) as tmp:
            stage=Path(tmp)
            for member in members:
                dst=stage/member.name;dst.parent.mkdir(parents=True,exist_ok=True)
                with archive.extractfile(member) as src,dst.open('wb') as out:shutil.copyfileobj(src,out)
                dst.chmod(member.mode & 0o777 & ~0o022)
            count=verify_game(stage,manifest)
            (root/'games').mkdir(exist_ok=True)
            # Refuse to replace a concurrently installed or modified game.
            if (root/'games'/game).exists():raise FileExistsError(game)
            (stage/'games'/game).rename(root/'games'/game)
            (root/'assets/manifests').mkdir(parents=True,exist_ok=True)
            shutil.copyfile(stage/manifest_name,root/manifest_name)
        print(f'{game}: installed and verified {count} files',flush=True)

def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--games',nargs='+');p.add_argument('--root',type=Path,default=ROOT);p.add_argument('--verify-only',action='store_true');a=p.parse_args()
    rows=json.loads((ROOT/'assets/manifest.json').read_text())['games']
    known={r['game'] for r in rows}
    if a.games and set(a.games)-known:p.error('Unknown games: '+','.join(sorted(set(a.games)-known)))
    for row in rows:
        if not a.games or row['game'] in a.games:install_game(a.root.resolve(),row,a.verify_only)
if __name__=='__main__':main()
