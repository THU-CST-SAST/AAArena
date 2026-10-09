"""Compatibility for the verified LostSpace Python SDK turn dispatcher."""
from __future__ import annotations

import ast
import hashlib
import json
import shutil
from pathlib import Path

from .build_sandbox import BuildSandboxError, validate_build_tree
from .buildcache import published_build_dir
from .python_sdk import _files, _sha

_MAIN_SHA = '543cf49fdb3a24d462bcd63d1fd5f1ce8dcffc19bd67f6c503828171c48cf84a'
_POLICY = 'lostspace-escape-waiting-dispatch-v1'
_MANIFEST = '.aa-sdk-runtime.json'


def _repair_dispatch(text: str) -> str:
    old = 'if self.__player.status != STATUS.ALIVE.value:'
    if text.count(old) != 1:
        raise OSError('Unsupported LostSpace SDK dispatcher')
    fixed = text.replace(old, 'if self.__player.status not in (STATUS.ALIVE.value, 4):')
    before = ast.parse(text)
    after = ast.parse(fixed)
    # Check the entire AST after restoring the one SDK method, including all
    # strategy methods, module statements and class configuration.
    original = [n for n in ast.walk(before) if isinstance(n, ast.FunctionDef) and n.name == '__start_turn']
    changed = [n for n in ast.walk(after) if isinstance(n, ast.FunctionDef) and n.name == '__start_turn']
    if len(original) != 1 or len(changed) != 1 or ast.dump(original[0]) == ast.dump(changed[0]):
        raise OSError('Unsupported LostSpace SDK dispatcher structure')
    class Restore(ast.NodeTransformer):
        def visit_FunctionDef(self, node):
            return original[0] if node is changed[0] else self.generic_visit(node)
    if ast.dump(before) != ast.dump(Restore().visit(after)):
        raise OSError('SDK repair would modify policy logic')
    return fixed


def prepare_lostspace_sdk(source: Path, cache_root: Path) -> Path:
    """Keep original files; allow actions during the referee's waiting state.

    Only the exact verified SDK-containing source is eligible. Other submissions
    and SDK versions retain their own turn dispatch behavior.
    """
    source = Path(source).resolve()
    main = source / 'main.py'
    if not main.is_file() or _sha(main) != _MAIN_SHA:
        return source
    try:
        validate_build_tree(source)
    except BuildSandboxError as exc:
        raise OSError(f'SDK source tree validation failed: {exc}') from exc
    original = _files(source)
    if _MANIFEST in original or '.aa_arena-complete' in original:
        raise OSError('SDK source contains reserved runtime metadata')
    fixed = _repair_dispatch(main.read_text()).encode()
    expected = dict(original, **{'main.py': hashlib.sha256(fixed).hexdigest()})
    key = hashlib.sha256(json.dumps({'policy': _POLICY, 'files': original}, sort_keys=True).encode()).hexdigest()
    target = Path(cache_root) / ('sdk-' + key)
    with published_build_dir(target, source=source, ignore=shutil.ignore_patterns('.git', '__pycache__', '*.pyc')) as (work, reused):
        if not reused:
            if _files(work) != original:
                raise OSError('SDK source changed during preparation')
            (work / 'main.py').write_bytes(fixed)
            if _files(work) != expected or _files(source) != original:
                raise OSError('Unexpected SDK runtime-copy delta')
            (work / _MANIFEST).write_text(json.dumps({
                'policy': _POLICY, 'source_files': original, 'runtime_files': expected,
                'changed_files': ['main.py'], 'changed_function': '__start_turn',
                'policy_logic_preserved': True, 'original_sources_preserved': True,
            }, indent=2) + '\n')
    manifest = json.loads((target / _MANIFEST).read_text())
    actual = _files(target)
    actual.pop(_MANIFEST, None)
    actual.pop('.aa_arena-complete', None)
    if manifest.get('policy') != _POLICY or manifest.get('source_files') != original or manifest.get('runtime_files') != expected or actual != expected:
        raise OSError('SDK runtime-copy integrity check failed')
    return target
