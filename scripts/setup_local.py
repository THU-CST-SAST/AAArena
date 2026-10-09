#!/usr/bin/env python3
"""Install and validate the documented Linux runtime without model/API calls."""
from __future__ import annotations
import argparse
import json
import os
import platform
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def runtime_environment(root: Path) -> dict[str, str]:
    env = dict(os.environ)
    # A local installation must not accidentally inherit a research runtime.
    for key in ('PYTHONPATH', 'PYTHONHOME', 'AA_ARENA_REFERENCE_RUNTIME',
                'AA_ARENA_BACKEND_PYTHON', 'AA_ARENA_ADMISSION_ROOT'):
        env.pop(key, None)
    env.update(AA_ARENA_SYSTEMD_MODE='user', AA_ARENA_CPU_POLICY='unlimited',
               AA_ARENA_PLAYER_ENV=str(root / '.player-env'),
               AA_ARENA_CC='gcc-14', AA_ARENA_CXX='g++-14',
               PYTHONDONTWRITEBYTECODE='1', OPENBLAS_NUM_THREADS='1', OMP_NUM_THREADS='1')
    return env


def run(args, env, *, capture=False):
    return subprocess.run([str(x) for x in args], cwd=ROOT, env=env,
                          check=True, text=True, capture_output=capture)


def preflight(env):
    if platform.system() != 'Linux' or platform.machine() not in ('x86_64', 'amd64'):
        raise RuntimeError('Local matches require x86-64 Linux. Use a Linux host or VM; macOS/Windows native execution is not supported.')
    if sys.version_info < (3, 11):
        raise RuntimeError('Run setup with Python 3.11 or newer.')
    required = ('bwrap', 'systemctl', 'systemd-run', 'gcc-14', 'g++-14', 'make', 'cmake', 'ruby')
    missing = [x for x in required if shutil.which(x, path=env.get('PATH')) is None]
    if missing:
        raise RuntimeError('Install these OS prerequisites first: ' + ', '.join(missing))
    if not Path('/sys/fs/cgroup/cgroup.controllers').is_file():
        raise RuntimeError('The host requires cgroup v2.')
    # A degraded user manager can still create scopes; test the actual operation.
    run(['systemd-run', '--user', '--scope', '--quiet', '--', '/usr/bin/true'], env, capture=True)
    run(['bwrap', '--unshare-all', '--ro-bind', '/', '/', '--', '/usr/bin/true'], env, capture=True)
    for cc in ('gcc-14', 'g++-14'):
        version = run([cc, '-dumpfullversion'], env, capture=True).stdout.strip()
        if version != '14.2.0':
            raise RuntimeError(f'{cc} must be 14.2.0, got {version}')


def verify_player(env):
    python = ROOT / '.player-env/bin/python'
    probe = "import sys,json; print(json.dumps(list(sys.version_info[:3])))"
    version = json.loads(run([python, '-I', '-c', probe], env, capture=True).stdout)
    if version != [3, 10, 14]:
        raise RuntimeError(f'Player Python must be 3.10.14, got {version}')
    # Check every direct pin, including those not exercised by the starter policies.
    pins = [x.strip() for x in (ROOT / 'environment/player-requirements.txt').read_text().splitlines()
            if x.strip() and not x.startswith(('#', '--'))]
    code = ('import importlib.metadata as m,json,sys; '
            'pins=json.loads(sys.argv[1]); '
            'bad=[(p,m.version(p.split("==")[0])) for p in pins if m.version(p.split("==")[0]) != p.split("==")[1]]; '
            'print(json.dumps(bad)); sys.exit(bool(bad))')
    run([python, '-I', '-c', code, json.dumps(pins)], env)
    run([python, '-m', 'pip', 'check'], env)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--verify-only', action='store_true', help='Use existing environments, without installing packages; still run host and 12-game checks.')
    parser.add_argument('--jobs', type=int, default=2, help='Local validation concurrency (default: 2).')
    args = parser.parse_args()
    if args.jobs < 1:
        parser.error('--jobs must be positive')
    target = ROOT / 'validation/local-setup/activate.sh'
    target.unlink(missing_ok=True)
    env = runtime_environment(ROOT)
    preflight(env)
    controller = ROOT / '.venv/bin/python'
    player = ROOT / '.player-env/bin/python'
    if not args.verify_only:
        if not (ROOT / '.player-env').exists() and shutil.which('conda') is None:
            raise RuntimeError('Install Conda, or create .player-env with scripts/install_player_env.py using Python 3.10.14.')
        if not (ROOT / '.venv').exists():
            run([sys.executable, '-m', 'venv', str(ROOT / '.venv')], env)
        if not (ROOT / '.venv/pyvenv.cfg').is_file():
            raise RuntimeError('.venv is not a virtual environment; refusing to install into it.')
        version = run([controller, '-I', '-c', 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")'], env, capture=True).stdout.strip()
        command = [controller, '-m', 'pip', 'install', '-e', str(ROOT) + '[dev,benchmark,claude,games]']
        if version == '3.14':
            command += ['-c', ROOT / 'environment/controller-constraints-py314-linux.txt']
        run(command, env)
        if not (ROOT / '.player-env').exists():
            run(['conda', 'create', '-y', '--prefix', ROOT / '.player-env', '--override-channels', '-c', 'conda-forge', 'python=3.10.14', 'pip=24.3.1'], env)
            shutil.copyfile(ROOT / 'environment/player-environment-owner.json', ROOT / '.player-env/.aa-arena-player-environment.json')
        # Check prefix/version/ownership before touching an existing environment.
        run([controller, '-c', 'from aa_arena.core.player_environment import player_python; print(player_python())'], env)
        version = run([player, '-I', '-c', 'import sys; print(".".join(map(str,sys.version_info[:3])))'], env, capture=True).stdout.strip()
        if version != '3.10.14':
            raise RuntimeError(f'Player Python must be 3.10.14, got {version}')
        run([player, '-m', 'pip', 'install', '-r', ROOT / 'environment/player-requirements.txt'], env)
        run([controller, ROOT / 'scripts/install_assets.py'], env)
    verify_player(env)
    run([controller, '-m', 'pip', 'check'], env)
    run([controller, ROOT / 'scripts/install_assets.py', '--verify-only'], env)
    run([controller, ROOT / 'scripts/check_evaluator_host.py'], env)
    run([controller, ROOT / 'scripts/validate_games.py', '--jobs', args.jobs,
         '--output', ROOT / 'validation/local-setup'], env)
    # Emit only after actual matches pass; no credentials go into this shell file.
    lines = ['# Source this file from bash or zsh.', 'unset PYTHONPATH PYTHONHOME AA_ARENA_REFERENCE_RUNTIME AA_ARENA_BACKEND_PYTHON AA_ARENA_ADMISSION_ROOT',
             'source ' + shlex.quote(str(ROOT / '.venv/bin/activate'))]
    for key in ('AA_ARENA_SYSTEMD_MODE', 'AA_ARENA_CPU_POLICY', 'AA_ARENA_PLAYER_ENV',
                'AA_ARENA_CC', 'AA_ARENA_CXX', 'PYTHONDONTWRITEBYTECODE', 'OPENBLAS_NUM_THREADS', 'OMP_NUM_THREADS'):
        lines.append('export ' + key + '=' + shlex.quote(env[key]))
    target.write_text('\n'.join(lines) + '\n')
    print('Local installation and all 12 game contracts passed. For each shell: source ' + shlex.quote(str(target)))


if __name__ == '__main__':
    try:
        main()
    except (RuntimeError, OSError, subprocess.CalledProcessError) as exc:
        print('Local setup failed: ' + str(exc), file=sys.stderr)
        if isinstance(exc, subprocess.CalledProcessError):
            print((exc.stderr or exc.stdout or '')[-4000:], file=sys.stderr)
        raise SystemExit(1)
