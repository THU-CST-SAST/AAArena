import json
from pathlib import Path

import pytest

from aa_arena.replay.public_json import compact_replay, validate_public_replay


TRACE = ('Traceback (most recent call last):\n'
         '  File "/private/controller/backend/main.py", line 453, in apply\n'
         '    command[4] = min(2100000000, command[4])\n'
         'IndexError: list index out of range\n')


def test_generals_traceback_preserves_records_without_private_source(tmp_path: Path):
    first = {'Round': 0, 'Player': -1, 'Action': [8], 'Cells': [[0, -1]]}
    final = {'Round': 1, 'Player': 1, 'Action': [9], 'Content': 'ai 0 invalid operation'}
    source = tmp_path / 'raw.txt'
    source.write_text(json.dumps(first) + '\n' + TRACE + json.dumps(final) + '\n')
    public = tmp_path / 'public.json'
    compact_replay('generals', source, public)
    text = public.read_text()
    records = [r['record'] for r in json.loads(text)['rounds']]
    assert records[0] == first and records[-1] == final
    diagnostic = records[1]['_replay_diagnostic']
    assert diagnostic['kind'] == 'python_traceback'
    assert diagnostic['exception_type'] == 'IndexError'
    assert diagnostic['source_lines'] == [2, 5]
    assert '/private/' not in text and 'command[4]' not in text
    with pytest.raises(ValueError, match='backend diagnostic'):
        validate_public_replay('generals', source, public)


@pytest.mark.parametrize('middle', [
    'unexpected garbage\n',
    '{broken json}\n',
    'Traceback (most recent call last):\n  File "private.py", line 3\n',
])
def test_generals_rejects_unrecognised_or_incomplete_records(tmp_path: Path, middle):
    source = tmp_path / 'raw.txt'
    source.write_text('{"Round": 0}\n' + middle + '{"Round": 1}\n')
    with pytest.raises(ValueError):
        compact_replay('generals', source, tmp_path / 'public.json')


def test_rollman_does_not_accept_generals_traceback_extension(tmp_path: Path):
    source = tmp_path / 'raw.txt'
    source.write_text('{"round": 0}\n' + TRACE + '{"round": 1}\n')
    with pytest.raises(ValueError):
        compact_replay('rollman', source, tmp_path / 'public.json')
