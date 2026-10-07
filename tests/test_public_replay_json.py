from __future__ import annotations

import json
from pathlib import Path

from aa_arena.replay.public_json import MAX_PUBLIC_REPLAY_BYTES, compact_replay


def test_compact_replay_keeps_round_actions_and_bounds_nested_state(tmp_path: Path) -> None:
    source = tmp_path / "raw.json"
    source.write_text(
        json.dumps(
            [
                {"op0": [{"type": 1}], "op1": [{"type": 2}], "round_state": {"coins": [1, 2]}},
                {"op0": [], "op1": [], "round_state": {"coins": [3, 4], "renderer": "x" * 10000}},
            ]
        ),
        encoding="utf-8",
    )
    destination = tmp_path / "public" / "replay.json"
    metadata = compact_replay("antwar", source, destination)
    document = json.loads(destination.read_text(encoding="utf-8"))

    assert metadata["source_size"] == source.stat().st_size
    assert document["rounds"][0]["record"]["op0"][0]["type"] == 1
    assert destination.stat().st_size <= MAX_PUBLIC_REPLAY_BYTES


def test_compact_replay_decodes_miracle_binary_to_json(tmp_path: Path) -> None:
    source = tmp_path / "miracle.bin"
    source.write_bytes(
        b"".join(
            int(value).to_bytes(4, "big", signed=True)
            for value in [0, 0, 0, 1, 2, 0, 0, 3, 10, 1, 0, 0, 0, 0]
        )
    )
    destination = tmp_path / "replay.json"
    compact_replay("miracle", source, destination)
    document = json.loads(destination.read_text(encoding="utf-8"))

    assert document["format"] == "miracle-int32-events"
    assert document["rounds"][0]["events"][0]["event_name"] == "GameEnd"



def test_jsonl_timeline_and_terminal_survive_size_limit(tmp_path):
    from aa_arena.replay.public_json import validate_public_replay
    for game in ('generals', 'rollman'):
        records = [{'round': i, 'cells': [[0, -1, 0], [1, 2, 3]],
                    'state': 'x' * 500, 'score': [0, i]} for i in range(2000)]
        source = tmp_path / (game + '.jsonl')
        source.write_text('\n'.join(json.dumps(r) for r in records))
        public = tmp_path / (game + '.json')
        compact_replay(game, source, public)
        doc = json.loads(public.read_text())
        retained = [r['record'] for r in doc['rounds']]
        assert records[0] in retained and records[1000] in retained and records[-1] in retained
        assert retained[0]['cells'] == [[0, -1, 0], [1, 2, 3]]
        assert public.stat().st_size <= MAX_PUBLIC_REPLAY_BYTES
        validate_public_replay(game, source, public)
        doc['rounds'] = doc['rounds'][:1]
        public.write_text(json.dumps(doc))
        import pytest
        with pytest.raises(ValueError, match='lost source record'):
            validate_public_replay(game, source, public)


def test_certification_rejects_player_error_even_with_nonempty_rounds(tmp_path):
    import pytest
    from aa_arena.replay.public_json import validate_public_replay
    source = tmp_path / 'snake.json'
    source.write_text(json.dumps({'operations': [{'type': 1}], 'round_info': [{}],
                                 'end_info': {'type': 'PLAYER_ERROR', 'err': 'timeOutError'}}))
    public = tmp_path / 'public.json'
    compact_replay('snakego', source, public)
    with pytest.raises(ValueError, match='player error'):
        validate_public_replay('snakego', source, public)


def test_complete_array_still_ignores_appended_renderer(tmp_path):
    source = tmp_path / 'raw.json'
    source.write_text('[{"op0": [], "round_state": {"coins": [0, 5]}}]\n{"renderer": "private"}')
    public = tmp_path / 'public.json'
    compact_replay('antwar', source, public)
    assert 'private' not in public.read_text()
    assert json.loads(public.read_text())['rounds'][0]['record']['round_state']['coins'] == [0, 5]
