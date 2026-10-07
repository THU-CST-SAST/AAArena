"""Independent model profiles and isolated Codex configuration generation."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path

from aa_arena.io import atomic_write_json, atomic_write_text


REPOSITORY_ROOT = Path(__file__).resolve().parents[3]


@dataclass(frozen=True)
class ModelProfile:
    name: str
    model: str
    model_provider: str
    base_url: str
    api_key_env: str
    reasoning_effort: str
    wire_api: str
    context_window: int
    effective_context_window_percent: int
    claude_auth_mode: str = "api_key"
    stream_max_retries: int | None = None

    def __post_init__(self) -> None:
        if self.stream_max_retries is not None and (type(self.stream_max_retries) is not int or self.stream_max_retries < 0):
            raise ValueError("stream_max_retries must be a nonnegative integer or null")
        if self.claude_auth_mode not in {"api_key", "bearer"}:
            raise ValueError("claude_auth_mode must be api_key or bearer")


def load_profile(name: str, root: Path | None = None) -> ModelProfile:
    repository = (root or REPOSITORY_ROOT).resolve()
    if not name or Path(name).name != name or name in {".", ".."}:
        raise ValueError("model profile must be a simple name")
    directory = Path(os.environ.get("AA_ARENA_PROFILE_DIR", str(repository / "model_profiles"))).expanduser()
    path = directory / f"{name}.json"
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return ModelProfile(
            name=str(value["name"]),
            model=str(value["model"]),
            model_provider=str(value["model_provider"]),
            base_url=str(value["base_url"]),
            api_key_env=str(value["api_key_env"]),
            reasoning_effort=str(value["reasoning_effort"]),
            wire_api=str(value["wire_api"]),
            context_window=int(value["context_window"]),
            effective_context_window_percent=int(value["effective_context_window_percent"]),
            claude_auth_mode=str(value.get("claude_auth_mode", "api_key")),
            stream_max_retries=value.get("stream_max_retries"),
        )
    except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ValueError(f"invalid model profile: {path}") from exc


def _catalog(profile: ModelProfile) -> dict[str, object]:
    return {
        "models": [
            {
                "slug": profile.model,
                "display_name": profile.model,
                "description": "AA-Arena native Codex benchmark model",
                "default_reasoning_level": profile.reasoning_effort,
                "supported_reasoning_levels": [
                    {"effort": "low", "description": "Light reasoning"},
                    {"effort": "high", "description": "Enhanced reasoning"},
                    {"effort": "max", "description": "Deep reasoning"},
                ],
                "shell_type": "shell_command",
                "visibility": "list",
                "supported_in_api": True,
                "priority": 0,
                "base_instructions": "",
                "supports_reasoning_summaries": True,
                "default_reasoning_summary": "none",
                "support_verbosity": False,
                "apply_patch_tool_type": "freeform",
                "truncation_policy": {"mode": "bytes", "limit": 32768},
                "context_window": profile.context_window,
                "max_context_window": profile.context_window,
                "effective_context_window_percent": profile.effective_context_window_percent,
                "supports_parallel_tool_calls": True,
                "experimental_supported_tools": [],
                "input_modalities": ["text"],
            }
        ]
    }


def write_codex_home(
    codex_home: Path,
    profile: ModelProfile,
    *,
    workspace_path: str = "/workspace",
    codex_home_path: str = "/codex-home",
    provider_base_url: str | None = None,
) -> Path:
    codex_home = Path(codex_home).resolve()
    codex_home.mkdir(parents=True, exist_ok=True)
    catalog = codex_home / "models.json"
    atomic_write_json(catalog, _catalog(profile))
    quote = json.dumps
    text = "\n".join(
        (
            f"model = {quote(profile.model)}",
            f"model_provider = {quote(profile.model_provider)}",
            f"model_reasoning_effort = {quote(profile.reasoning_effort)}",
            "model_auto_compact_token_limit = 200000",
            'model_auto_compact_token_limit_scope = "total"',
            "tool_output_token_limit = 8192",
            'sandbox_mode = "workspace-write"',
            'web_search = "disabled"',
            f"model_catalog_json = {quote(str(Path(codex_home_path) / 'models.json'))}",
            'default_permissions = "arena"',
            "",
            f"[model_providers.{profile.model_provider}]",
            f"name = {quote(profile.name)}",
            f"base_url = {quote(provider_base_url or profile.base_url)}",
            f"wire_api = {quote(profile.wire_api)}",
            'env_key = "OPENAI_API_KEY"',
            "requires_openai_auth = true",
            # Transport retries are an explicit user setting, independent of model identity.
            *((f'stream_max_retries = {profile.stream_max_retries}',)
              if profile.stream_max_retries is not None else ()),
            *(('request_max_retries = 0', 'stream_idle_timeout_ms = 3600000')
              if int(os.environ.get('AA_ARENA_PROXY_RATE_LIMIT_RETRIES','0')) > 0 else ()),
            "",
            "[features]",
            "goals = true",
            "remote_compaction_v2 = true",
            "apps = false",
            "auth_elicitation = false",
            "browser_use = false",
            "browser_use_external = false",
            "browser_use_full_cdp_access = false",
            "computer_use = false",
            "image_generation = false",
            "in_app_browser = false",
            "multi_agent = false",
            "plugins = false",
            "remote_plugin = false",
            "recommended_plugins = false",
            "skill_mcp_dependency_install = false",
            "skill_search = false",
            "tool_call_mcp_elicitation = false",
            "tool_suggest = false",
            "view_image = false",
            "workspace_dependencies = false",
            "",
            "[analytics]",
            "enabled = false",
            "",
            "[shell_environment_policy]",
            'inherit = "none"',
            'set = { PATH = "/usr/bin:/bin", LANG = "C.UTF-8", HOME = "/codex-home/home" }',
            (f'exclude = ["OPENAI_API_KEY", {quote(profile.api_key_env)}, "CODEX_HOME"]'),
            "",
            "[permissions.arena]",
            'description = "AA-Arena isolated candidate workspace"',
            "",
            "[permissions.arena.filesystem]",
            '":root" = "deny"',
            '":minimal" = "read"',
            '":tmpdir" = "write"',
            '":slash_tmp" = "write"',
            '"/opt/codex" = "read"',
            f'{quote(workspace_path)} = "read"',
            f'{quote(str(Path(workspace_path) / "strategy"))} = "write"',
            f'{quote(str(Path(workspace_path) / "skills"))} = "write"',
            f'{quote(str(Path(workspace_path) / "notes"))} = "write"',
            f'{quote(str(Path(workspace_path) / "replays"))} = "write"',
            f'{quote(str(Path(workspace_path) / "artifacts"))} = "write"',
            "",
            "[permissions.arena.network]",
            "enabled = false",
            "",
        )
    )
    config = codex_home / "config.toml"
    atomic_write_text(config, text)
    return config
