from pathlib import Path
import subprocess
from aa_arena.core.cpp_build import compile_cpp_package


def test_nested_sdk_with_precompiled_main_is_rebuilt(tmp_path):
    source = tmp_path / 'strategy'
    nested = source / 'public_sdk_cpp'
    nested.mkdir(parents=True)
    (nested/'main.cpp').write_text('int main(){return 0;}\n')
    (nested/'makefile').write_text('all: main\nmain: main.cpp\n\tg++ -O2 -o main main.cpp\n')
    subprocess.run(['make'], cwd=nested, check=True, capture_output=True)
    before = (nested/'main').read_bytes()
    exe = compile_cpp_package(source, tmp_path/'build')
    assert exe.is_file()
    assert subprocess.run([str(exe)]).returncode == 0
    assert (nested/'main').read_bytes() == before, 'Workspace must remain untouched'
