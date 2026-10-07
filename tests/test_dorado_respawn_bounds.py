from pathlib import Path
import os
import subprocess


def test_respawn_rejects_outside_map_before_height_access(tmp_path):
    repo=Path(__file__).resolve().parents[1]
    root=repo/'games/dorado/backend'
    sources=sorted(p for p in (root/'logic/src').glob('*.cpp') if p.name != 'main.cpp')
    sources += sorted((root/'logic/src').glob('*.cc'))
    sources += sorted((root/'liblogic').glob('*.cc'))
    sources += sorted((root/'platform_shared').glob('*.cc'))
    binary=tmp_path/'respawn-test'
    command=['g++','-std=c++14','-O1','-g','-w','-D_SERVER_',
        '-fsanitize=address','-fno-omit-frame-pointer','-pthread',
        *[arg for name in ('logic/src/include','platform_shared/include','shared','liblogic/include')
            for arg in ('-I',str(root/name))],
        str(repo/'tests/fixtures/dorado_respawn_bounds.cc'),*[str(p) for p in sources],
        '-ldl','-lrt','-o',str(binary)]
    subprocess.run(command,check=True,capture_output=True,timeout=180)
    subprocess.run([str(binary)],cwd=tmp_path,check=True,capture_output=True,timeout=20,
        env=dict(os.environ, ASAN_OPTIONS='detect_leaks=0'))
