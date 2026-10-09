"""Version-pinned Python SDK adapters in immutable, auditable runtime copies."""
from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path

from .build_sandbox import BuildSandboxError, validate_build_tree
from .buildcache import published_build_dir

_STATE = 'SDK/backend/state.py'
_ENGINE = 'SDK/backend/engine.py'
_STATE_SHA = '93114a0ff0d29673efd7f6e692103159ede717e46bcb9712b9044c00de2d71d7'
_ENGINE_SHA = 'fdf2c49e0e4502400de9970e9c5468ac46a99e5f50269dbf481a77fa2f2dda6f'
_POLICY = 'antwar2-refund-forwarding-v1'
_MANIFEST = '.aa-sdk-runtime.json'
_IGNORED = {'.git', '__pycache__'}


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _files(root: Path) -> dict[str, str]:
    return {
        p.relative_to(root).as_posix(): _sha(p)
        for p in sorted(root.rglob('*'))
        if p.is_file() and not (_IGNORED & set(p.relative_to(root).parts))
        and p.suffix != '.pyc'
    }


def prepare_antwar2_sdk(source: Path, cache_root: Path) -> Path:
    """Forward the optional tower only for the verified adapter/engine pair.

    Strategy and original SDK files are never written. Unknown SDK versions use
    their own implementation. Every runtime-copy delta is recorded and checked.
    """
    source = Path(source).resolve()
    if not all((source / name).is_file() for name in (_STATE, _ENGINE)):
        return source
    if _sha(source / _STATE) != _STATE_SHA or _sha(source / _ENGINE) != _ENGINE_SHA:
        return source
    try:
        validate_build_tree(source)
    except BuildSandboxError as exc:
        raise OSError(f"SDK source tree validation failed: {exc}") from exc
    original = _files(source)
    if _MANIFEST in original or ".aa_arena-complete" in original:
        raise OSError("SDK source contains reserved runtime metadata")
    payload = json.dumps({'policy': _POLICY, 'files': original}, sort_keys=True).encode()
    target = Path(cache_root) / ('sdk-' + hashlib.sha256(payload).hexdigest())
    with published_build_dir(
        target, source=source,
        ignore=shutil.ignore_patterns('.git', '__pycache__', '*.pyc'),
    ) as (work, reused):
        if not reused:
            if _files(work) != original:
                raise OSError('SDK runtime source changed during preparation')
            state = work / _STATE
            text = state.read_text()
            text = text.replace(
                'def downgrade_tower_income(self, tower_type) -> int:',
                'def downgrade_tower_income(self, tower_type, tower: Tower | None = None) -> int:',
            ).replace(
                'return self._state.downgrade_tower_income(tower_type)',
                'return self._state.downgrade_tower_income(tower_type, tower)',
            )
            state.write_text(text)
            if _sha(state) != "5fc54d9d137700334ad7cc625d3ba68ad5b7df21510893b9dab3887fbfda58e9":
                raise OSError("SDK adapter differs from the validated public API")
            runtime = _files(work)
            if [name for name in original if runtime.get(name) != original[name]] != [_STATE]:
                raise OSError('Unexpected SDK runtime-copy delta')
            if set(runtime) != set(original) or _files(source) != original:
                raise OSError('SDK source or file set changed during preparation')
            (work / _MANIFEST).write_text(json.dumps({
                'policy': _POLICY, 'source_files': original,
                'runtime_files': runtime, 'changed_files': [_STATE],
                'original_sources_preserved': True,
            }, indent=2) + '\n')
    manifest = json.loads((target / _MANIFEST).read_text())
    actual = _files(target)
    actual.pop(_MANIFEST, None)
    actual.pop('.aa_arena-complete', None)
    if manifest['source_files'] != original or manifest['runtime_files'] != actual:
        raise OSError('SDK runtime-copy integrity check failed')
    return target
