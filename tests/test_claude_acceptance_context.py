"""Acceptance intent and automatic compaction evidence survive SDK restarts."""
import json
from types import SimpleNamespace
from aa_arena.benchmark.claude_runtime import ClaudeArenaRuntime


def test_acceptance_objective_does_not_start_strategy_experiment(tmp_path):
    runtime=ClaudeArenaRuntime(SimpleNamespace(controller=tmp_path),model='fixture',base_url='http://localhost',token='fixture',acceptance=True)
    objective=runtime.objective()
    assert 'Execute only the steps explicitly requested' in objective
    assert 'Do not submit any match until requested' in objective


def test_restored_automatic_and_manual_boundaries_are_not_double_counted(tmp_path):
    auto={'compact_metadata':{'trigger':'auto'},'uuid':'auto-1'}
    manual={'compact_metadata':{'trigger':'manual'},'uuid':'manual-1'}
    rows=[{'type':'sdk_event','event':{'subtype':'compact_boundary','data':auto}},
          {'type':'sdk_event','event':{'subtype':'compact_boundary','data':manual}},
          {'type':'compaction_verified','boundary':manual}]
    (tmp_path/'claude-events.jsonl').write_text(''.join(json.dumps(x)+'\n' for x in rows))
    runtime=ClaudeArenaRuntime(SimpleNamespace(controller=tmp_path),model='fixture',base_url='http://localhost',token='fixture')
    assert runtime.compactions==[auto,manual]
