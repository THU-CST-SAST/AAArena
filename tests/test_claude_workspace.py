"""Real sandbox write/read/rename probes against all assigned games' SDK copies."""
import argparse
import json
import shutil
import tempfile
from pathlib import Path
from types import SimpleNamespace
from aa_arena.benchmark.claude_runtime import ClaudeArenaRuntime, BenchmarkService

from aa_arena.resources import build_bundle
import pytest
GAMES=json.loads((Path(__file__).parents[1]/'configs/paper.json').read_text())['games']


def check(game):
    with tempfile.TemporaryDirectory(prefix='harness-workspace-') as directory:
        workspace=Path(directory)
        resources=build_bundle(game,workspace/'bundles')
        shutil.copytree(resources/'sdk',workspace/'strategy')
        for name in ['skills','notes','replays','artifacts','resources']:
            (workspace/name).mkdir()
        (workspace/'resources/readonly.txt').write_text('immutable')
        state={'status':'running'}
        service=SimpleNamespace(controller=workspace,workspace=workspace,ledger=SimpleNamespace(state=lambda:state))
        runtime=ClaudeArenaRuntime(service,model='test-model-e',base_url='unused',token='unused')
        runtime.prepare_working_tree()
        command="""python3 - <<'PY'
from pathlib import Path
root=Path('strategy')
files=[p for p in root.rglob('*') if p.is_file() and not p.is_symlink()]
assert files
for p in files:
    value=p.read_bytes();p.write_bytes(value)
probe=root/'write-probe';probe.write_text('ok');probe.rename(root/'rename-probe')
assert (root/'rename-probe').read_text()=='ok';(root/'rename-probe').unlink()
try: Path('resources/readonly.txt').write_text('bad')
except OSError: pass
else: raise AssertionError('Read-only resources were writable')
print('strategy-files-writable',len(files),'resources-protected')
PY"""
        result=BenchmarkService.tool_call(service,'workspace_shell',{'command':command})
        assert result['exit_code']==0,result
        return {'game':game,'passed':True,'output':result['output'].strip()}


@pytest.mark.sandbox_integration
@pytest.mark.parametrize('game',GAMES)
def test_sdk_workspace(game):
    import os
    if os.environ.get('AA_ARENA_RUN_ROOTLESS_INTEGRATION')!='1':
        pytest.skip('Set AA_ARENA_RUN_ROOTLESS_INTEGRATION=1 on a configured Linux host')
    assert check(game)['passed']

if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--games',nargs='+',choices=GAMES,default=GAMES);a=p.parse_args()
    print(json.dumps({'checks':[check(game) for game in a.games]}))
