from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from aa_arena.core.native_sdk import (
    NativeSdkCatalog,
    NativeSdkCatalogError,
    apply_sdk_overlay,
)


def _write_catalog(
    game_dir: Path,
    *,
    bundle_id: str = "sdk-v1",
    fingerprint: str = "1" * 64,
    requested_path: str = "sdk/header.hpp",
    source_name: str = "include/header.hpp",
    content: bytes = b"public sdk bytes\n",
    extra_bundles: list[dict[str, object]] | None = None,
) -> Path:
    sdk_root = game_dir / "public_sdk_cpp"
    source = sdk_root / source_name
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_bytes(content)
    bundles: list[dict[str, object]] = [
        {
            "bundle_id": bundle_id,
            "fingerprint_sha256": fingerprint,
            "files": [
                {
                    "requested_path": requested_path,
                    "source": source_name,
                    "sha256": hashlib.sha256(content).hexdigest(),
                }
            ],
        }
    ]
    bundles.extend(extra_bundles or [])
    catalog_path = sdk_root / "native_sdk_catalog.json"
    catalog_path.write_text(
        json.dumps(
            {
                "schema_version": "1.0",
                "game": game_dir.name,
                "bundles": bundles,
            }
        ),
        encoding="utf-8",
    )
    return catalog_path


def test_load_resolves_one_exact_declared_sdk_file(tmp_path: Path) -> None:
    game_dir = tmp_path / "game"
    _write_catalog(game_dir)

    catalog = NativeSdkCatalog.load(game_dir)
    resolution = catalog.resolve_missing("sdk/header.hpp")

    assert resolution is not None
    assert resolution.bundle_id == "sdk-v1"
    assert resolution.bundle_fingerprint == "1" * 64
    assert resolution.requested_path == "sdk/header.hpp"
    assert resolution.source_path == game_dir / "public_sdk_cpp/include/header.hpp"
    assert resolution.sha256 == hashlib.sha256(b"public sdk bytes\n").hexdigest()


def test_overlay_copies_a_missing_file_and_reports_provenance(tmp_path: Path) -> None:
    game_dir = tmp_path / "game"
    _write_catalog(game_dir)
    resolution = NativeSdkCatalog.load(game_dir).resolve_missing("sdk/header.hpp")
    assert resolution is not None
    staging = tmp_path / "staging"

    applied = apply_sdk_overlay(staging, resolution)

    destination = staging / "sdk/header.hpp"
    assert destination.read_bytes() == b"public sdk bytes\n"
    assert applied.copied is True
    assert applied.bundle_id == "sdk-v1"
    assert applied.destination_path == destination
    assert applied.sha256 == hashlib.sha256(b"public sdk bytes\n").hexdigest()


def test_overlay_never_overwrites_an_existing_player_file(tmp_path: Path) -> None:
    game_dir = tmp_path / "game"
    _write_catalog(game_dir)
    resolution = NativeSdkCatalog.load(game_dir).resolve_missing("sdk/header.hpp")
    assert resolution is not None
    destination = tmp_path / "staging/sdk/header.hpp"
    destination.parent.mkdir(parents=True)
    destination.write_bytes(b"player-owned\n")

    applied = apply_sdk_overlay(tmp_path / "staging", resolution)

    assert applied.copied is False
    assert destination.read_bytes() == b"player-owned\n"


def test_overlay_rejects_a_parent_symlink_that_escapes_staging(
    tmp_path: Path,
) -> None:
    game_dir = tmp_path / "game"
    _write_catalog(game_dir)
    resolution = NativeSdkCatalog.load(game_dir).resolve_missing("sdk/header.hpp")
    assert resolution is not None
    staging = tmp_path / "staging"
    staging.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (staging / "sdk").symlink_to(outside, target_is_directory=True)

    with pytest.raises(NativeSdkCatalogError, match="escapes staging"):
        apply_sdk_overlay(staging, resolution)

    assert list(outside.iterdir()) == []


@pytest.mark.parametrize(
    "requested_path",
    (
        "/include/x.h",
        "../x.h",
        "a/../../x.h",
        r"jsoncpp\json\json.h",
        "",
    ),
)
def test_resolve_rejects_unsafe_requested_paths(
    tmp_path: Path, requested_path: str
) -> None:
    game_dir = tmp_path / "game"
    _write_catalog(game_dir)
    catalog = NativeSdkCatalog.load(game_dir)

    assert catalog.resolve_missing(requested_path) is None


def test_resolve_returns_none_for_unknown_file(tmp_path: Path) -> None:
    game_dir = tmp_path / "game"
    _write_catalog(game_dir)

    assert NativeSdkCatalog.load(game_dir).resolve_missing("unknown.hpp") is None


def test_ambiguous_file_requires_an_exact_bundle_fingerprint(tmp_path: Path) -> None:
    game_dir = tmp_path / "game"
    second_content = b"other sdk bytes\n"
    second_source = game_dir / "public_sdk_cpp/include-v2/header.hpp"
    second_source.parent.mkdir(parents=True, exist_ok=True)
    second_source.write_bytes(second_content)
    second_fingerprint = "2" * 64
    _write_catalog(
        game_dir,
        extra_bundles=[
            {
                "bundle_id": "sdk-v2",
                "fingerprint_sha256": second_fingerprint,
                "files": [
                    {
                        "requested_path": "sdk/header.hpp",
                        "source": "include-v2/header.hpp",
                        "sha256": hashlib.sha256(second_content).hexdigest(),
                    }
                ],
            }
        ],
    )
    catalog = NativeSdkCatalog.load(game_dir)

    assert catalog.resolve_missing("sdk/header.hpp") is None
    selected = catalog.resolve_missing(
        "sdk/header.hpp", fingerprint=second_fingerprint
    )
    assert selected is not None
    assert selected.bundle_id == "sdk-v2"
    assert selected.source_path == second_source


@pytest.mark.parametrize(
    "field", ("bundle_id", "fingerprint", "requested_path", "content")
)
def test_catalog_identity_covers_build_relevant_metadata(
    tmp_path: Path, field: str
) -> None:
    first = tmp_path / "first/game"
    second = tmp_path / "second/game"
    first_options: dict[str, object] = {}
    second_options: dict[str, object] = {}
    if field == "bundle_id":
        second_options["bundle_id"] = "sdk-v2"
    elif field == "fingerprint":
        second_options["fingerprint"] = "2" * 64
    elif field == "requested_path":
        second_options["requested_path"] = "other/header.hpp"
    else:
        second_options["content"] = b"changed sdk bytes\n"
    _write_catalog(first, **first_options)
    _write_catalog(second, **second_options)

    assert NativeSdkCatalog.load(first).identity != NativeSdkCatalog.load(second).identity


def test_catalog_load_rejects_a_hash_mismatch(tmp_path: Path) -> None:
    game_dir = tmp_path / "game"
    catalog_path = _write_catalog(game_dir)
    catalog = json.loads(catalog_path.read_text(encoding="utf-8"))
    catalog["bundles"][0]["files"][0]["sha256"] = "0" * 64
    catalog_path.write_text(json.dumps(catalog), encoding="utf-8")

    with pytest.raises(NativeSdkCatalogError, match="hash mismatch"):
        NativeSdkCatalog.load(game_dir)


def test_catalog_load_rejects_a_source_outside_public_sdk_roots(
    tmp_path: Path,
) -> None:
    game_dir = tmp_path / "game"
    catalog_path = _write_catalog(game_dir)
    catalog = json.loads(catalog_path.read_text(encoding="utf-8"))
    catalog["bundles"][0]["source_root"] = "../outside"
    catalog_path.write_text(json.dumps(catalog), encoding="utf-8")

    with pytest.raises(NativeSdkCatalogError, match="source_root"):
        NativeSdkCatalog.load(game_dir)
