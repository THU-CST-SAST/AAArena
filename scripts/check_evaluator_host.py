"""Refuse evaluator startup when the player runtime or isolation is broken."""
import json
import tempfile
from pathlib import Path
from aa_arena.core.player_environment import player_python
from aa_arena.core.build_sandbox import run_isolated_build
from aa_arena.legacy.ai9 import _compiler_command, _compiler_identity
from aa_arena.sandbox.model import ProcessSpec
from aa_arena.sandbox.systemd import SystemdScopeLauncher
with tempfile.TemporaryDirectory(prefix='arena-host-check-') as directory:
    root=Path(directory); candidate=root/'candidate'; candidate.mkdir()
    private=root/'private'; private.write_text('private sentinel')
    (candidate/'probe.py').write_text(
        'import json,socket,sys,numpy\nfrom pathlib import Path\n'
        f'visible=Path({str(private)!r}).exists()\n'
        'network=True\n'
        'try:\n socket.create_connection(("1.1.1.1",53),timeout=2)\n'
        'except OSError:\n network=False\n'
        'print(json.dumps({"python":list(sys.version_info[:3]),"numpy":numpy.__version__,"private_visible":visible,"network":network}),flush=True)\n')
    proc=SystemdScopeLauncher(user_mode=True,cpu_policy='unlimited').start(
        ProcessSpec((str(player_python()),'probe.py'),candidate),match_id='evaluator-host-check',player_index=0)
    try:
        row=json.loads(proc.stdout.readline())
        assert row['python']==[3,10,14], row
        assert not row['private_visible'] and not row['network'], row
    finally:
        outcome=proc.cleanup()
        assert outcome.clean, outcome.detail
    print('Player runtime and file/network isolation verified')

with tempfile.TemporaryDirectory(prefix="arena-compiler-check-") as directory:
    root=Path(directory)
    (root/"probe.cpp").write_text("#include <iostream>\nint main(){std::cout << 42;return 0;}\n")
    identity = _compiler_identity()
    assert b"14.2.0" in identity, "Hosted reference evaluation requires GCC 14.2.0"
    result=run_isolated_build((_compiler_command(),"-std=c++14","probe.cpp","-o","probe"),cwd=root,timeout=30)
    assert result.returncode==0, result.stderr
    print("Isolated GCC 14.2.0 compiler verified")

# Dedicated evaluator-user startup only: a terminated controller can leave
# fail-closed admission leases. Reclaim them only after verifying that no live
# controller holds an active seat and all player cgroups are empty.
import os
from aa_arena.benchmark.admission import Gate, is_alive
admission_root = os.environ.get('AA_ARENA_ADMISSION_ROOT')
if admission_root:
    gate = Gate(admission_root)
    with gate.connect() as db:
        db.execute('BEGIN IMMEDIATE')
        active = db.execute("SELECT * FROM tickets WHERE state='active'").fetchall()
        stale = [row for row in active if not is_alive(row['owner'])]
        if stale:
            if any(is_alive(row['owner']) for row in active):
                raise RuntimeError('Stale admission leases coexist with a live controller; operator inspection required')
            group_root = Path(f'/sys/fs/cgroup/user.slice/user-{os.getuid()}.slice')
            if not group_root.is_dir():
                raise RuntimeError('Cannot verify player cgroup cleanup')
            for group in group_root.rglob('aa-arena-*.scope'):
                for processes in group.rglob('cgroup.procs'):
                    if processes.read_text().strip():
                        raise RuntimeError('Player cgroup remains populated; refusing to reclaim admission')
            for owner in {row['owner'] for row in stale}:
                db.execute('DELETE FROM tickets WHERE owner=?', (owner,))
            print(f'Verified player exit; reclaimed {len(stale)} stale admission seats')
