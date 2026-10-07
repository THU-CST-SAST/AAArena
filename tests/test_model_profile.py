from __future__ import annotations

from pathlib import Path
import json

from aa_arena.benchmark.profile import load_profile, write_codex_home
from aa_arena.cli import _compatible_record


def test_model_a_profile_is_native_responses_and_writes_isolated_codex_config(tmp_path: Path) -> None:
    profile = load_profile("test-model-a")
    assert profile.model == "test-model-a"
    assert profile.wire_api == "responses"
    assert profile.reasoning_effort == "max"
    assert profile.api_key_env == "ARENA_TEST_KEY"
    assert profile.context_window == 800000
    assert profile.effective_context_window_percent == 95
    config = write_codex_home(tmp_path, profile, provider_base_url="http://127.0.0.1:1234")
    text = config.read_text(encoding="utf-8")
    assert 'base_url = "http://127.0.0.1:1234"' in text
    assert "remote_compaction_v2 = true" in text
    assert 'web_search = "disabled"' in text
    assert "plugins = false" in text
    assert "browser_use = false" in text
    assert 'inherit = "none"' in text
    assert '"/workspace" = "read"' in text
    assert '"/workspace/strategy" = "write"' in text
    assert "AgentBench" not in text


def test_model_b_profile_is_native_responses_and_uses_own_host_key(tmp_path: Path) -> None:
    profile = load_profile("test-model-b")
    assert profile.model == "test-model-b"
    assert profile.model_provider == "fixture-provider"
    assert profile.api_key_env == "ARENA_TEST_KEY"
    assert profile.wire_api == "responses"
    assert profile.reasoning_effort == "max"
    text = write_codex_home(tmp_path, profile).read_text(encoding="utf-8")
    assert 'model = "test-model-b"' in text
    assert 'exclude = ["OPENAI_API_KEY", "ARENA_TEST_KEY", "CODEX_HOME"]' in text


def test_compatibility_cache_is_bound_to_runtime_identity(tmp_path: Path) -> None:
    path = tmp_path / "compatibility.json"
    identity = {"model": "test-model-a", "codex_version": "codex 1"}
    path.write_text(
        json.dumps({"identity": identity, "checks": {"resume": True, "compact": True}}),
        encoding="utf-8",
    )
    assert _compatible_record(path, identity) is True
    assert _compatible_record(path, {**identity, "codex_version": "codex 2"}) is False
