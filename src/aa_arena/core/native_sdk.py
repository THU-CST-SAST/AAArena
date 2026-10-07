"""Validated public-SDK catalogs and immutable staging overlays."""

from __future__ import annotations

import hashlib
import json
import shutil
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any


class NativeSdkCatalogError(RuntimeError):
    """A native SDK catalog or one of its pinned source files is invalid."""


@dataclass(frozen=True)
class SdkFile:
    requested_path: str
    source_path: Path
    sha256: str


@dataclass(frozen=True)
class SdkBundle:
    bundle_id: str
    fingerprint_sha256: str
    source_root: Path
    files: tuple[SdkFile, ...]


@dataclass(frozen=True)
class SdkResolution:
    bundle_id: str
    bundle_fingerprint: str
    requested_path: str
    source_path: Path
    sha256: str


@dataclass(frozen=True)
class AppliedOverlay:
    bundle_id: str
    requested_path: str
    source_path: Path
    destination_path: Path
    sha256: str
    copied: bool


@dataclass(frozen=True)
class AppliedRuntimeFile:
    requested_path: str
    source_path: Path
    destination_path: Path
    sha256: str
    copied: bool


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _safe_relative_posix(value: object, *, field: str) -> str:
    if not isinstance(value, str) or not value:
        raise NativeSdkCatalogError(f"{field} must be a non-empty string")
    if "\\" in value:
        raise NativeSdkCatalogError(f"{field} must use POSIX separators: {value!r}")
    path = PurePosixPath(value)
    if path.is_absolute() or ".." in path.parts:
        raise NativeSdkCatalogError(f"{field} must stay relative: {value!r}")
    if path.as_posix() != value or "." in path.parts:
        raise NativeSdkCatalogError(f"{field} must be normalized: {value!r}")
    if path.parts and ":" in path.parts[0]:
        raise NativeSdkCatalogError(f"{field} cannot be a drive path: {value!r}")
    return value


def _required_mapping(value: object, *, field: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise NativeSdkCatalogError(f"{field} must be an object")
    return value


def _required_list(value: object, *, field: str) -> list[object]:
    if not isinstance(value, list) or not value:
        raise NativeSdkCatalogError(f"{field} must be a non-empty array")
    return value


def _required_string(value: object, *, field: str) -> str:
    if not isinstance(value, str) or not value:
        raise NativeSdkCatalogError(f"{field} must be a non-empty string")
    return value


def _sha256_string(value: object, *, field: str) -> str:
    text = _required_string(value, field=field)
    if len(text) != 64 or any(character not in "0123456789abcdef" for character in text):
        raise NativeSdkCatalogError(f"{field} must be a lowercase SHA-256")
    return text


class NativeSdkCatalog:
    """One game's validated, provenance-pinned native SDK catalog."""

    def __init__(
        self,
        *,
        game_dir: Path,
        bundles: tuple[SdkBundle, ...],
        identity: str,
    ) -> None:
        self.game_dir = game_dir
        self.bundles = bundles
        self.identity = identity

    @classmethod
    def load(cls, game_dir: str | Path) -> NativeSdkCatalog:
        game_root = Path(game_dir).resolve()
        catalog_path = game_root / "public_sdk_cpp/native_sdk_catalog.json"
        try:
            payload = _required_mapping(
                json.loads(catalog_path.read_text(encoding="utf-8")),
                field="catalog",
            )
        except (OSError, json.JSONDecodeError) as exc:
            raise NativeSdkCatalogError(f"cannot load native SDK catalog: {catalog_path}") from exc

        if payload.get("schema_version") != "1.0":
            raise NativeSdkCatalogError("catalog schema_version must be '1.0'")
        if payload.get("game") != game_root.name:
            raise NativeSdkCatalogError(
                f"catalog game must be {game_root.name!r}, got {payload.get('game')!r}"
            )

        raw_bundles = _required_list(payload.get("bundles"), field="bundles")
        bundles: list[SdkBundle] = []
        bundle_ids: set[str] = set()
        canonical_bundles: list[dict[str, object]] = []
        for bundle_index, raw_bundle in enumerate(raw_bundles):
            bundle = _required_mapping(raw_bundle, field=f"bundles[{bundle_index}]")
            bundle_id = _required_string(
                bundle.get("bundle_id"), field=f"bundles[{bundle_index}].bundle_id"
            )
            if bundle_id in bundle_ids:
                raise NativeSdkCatalogError(f"duplicate bundle_id: {bundle_id}")
            bundle_ids.add(bundle_id)
            fingerprint = _sha256_string(
                bundle.get("fingerprint_sha256"),
                field=f"bundles[{bundle_index}].fingerprint_sha256",
            )
            source_root_text = _safe_relative_posix(
                bundle.get("source_root", "public_sdk_cpp"),
                field=f"bundles[{bundle_index}].source_root",
            )
            if not (
                source_root_text == "public_sdk_cpp"
                or source_root_text.startswith("public_sdk_cpp_compat/")
            ):
                raise NativeSdkCatalogError(
                    "source_root must be public_sdk_cpp or a public_sdk_cpp_compat subtree"
                )
            source_root = (game_root / source_root_text).resolve()
            try:
                source_root.relative_to(game_root)
            except ValueError as exc:
                raise NativeSdkCatalogError(
                    f"source_root escapes game directory: {source_root_text}"
                ) from exc
            if not source_root.is_dir():
                raise NativeSdkCatalogError(f"source_root does not exist: {source_root}")

            raw_files = _required_list(
                bundle.get("files"), field=f"bundles[{bundle_index}].files"
            )
            files: list[SdkFile] = []
            requested_in_bundle: set[str] = set()
            canonical_files: list[dict[str, str]] = []
            for file_index, raw_file in enumerate(raw_files):
                item = _required_mapping(
                    raw_file, field=f"bundles[{bundle_index}].files[{file_index}]"
                )
                requested_path = _safe_relative_posix(
                    item.get("requested_path"),
                    field=(
                        f"bundles[{bundle_index}].files[{file_index}].requested_path"
                    ),
                )
                if requested_path in requested_in_bundle:
                    raise NativeSdkCatalogError(
                        f"duplicate requested_path in {bundle_id}: {requested_path}"
                    )
                requested_in_bundle.add(requested_path)
                source_text = _safe_relative_posix(
                    item.get("source"),
                    field=f"bundles[{bundle_index}].files[{file_index}].source",
                )
                expected_hash = _sha256_string(
                    item.get("sha256"),
                    field=f"bundles[{bundle_index}].files[{file_index}].sha256",
                )
                source_path = (source_root / source_text).resolve()
                try:
                    source_path.relative_to(source_root)
                except ValueError as exc:
                    raise NativeSdkCatalogError(
                        f"SDK source escapes source_root: {source_text}"
                    ) from exc
                if not source_path.is_file() or source_path.is_symlink():
                    raise NativeSdkCatalogError(f"SDK source is not a regular file: {source_path}")
                actual_hash = _sha256_file(source_path)
                if actual_hash != expected_hash:
                    raise NativeSdkCatalogError(
                        f"SDK source hash mismatch for {source_path}: "
                        f"expected {expected_hash}, got {actual_hash}"
                    )
                files.append(SdkFile(requested_path, source_path, expected_hash))
                canonical_files.append(
                    {
                        "requested_path": requested_path,
                        "source_root": source_root_text,
                        "source": source_text,
                        "sha256": expected_hash,
                    }
                )
            bundles.append(
                SdkBundle(bundle_id, fingerprint, source_root, tuple(files))
            )
            canonical_bundles.append(
                {
                    "bundle_id": bundle_id,
                    "fingerprint_sha256": fingerprint,
                    "files": canonical_files,
                }
            )

        canonical = json.dumps(
            {
                "schema_version": "1.0",
                "game": game_root.name,
                "bundles": canonical_bundles,
            },
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        identity = hashlib.sha256(canonical).hexdigest()
        return cls(game_dir=game_root, bundles=tuple(bundles), identity=identity)

    def resolve_missing(
        self,
        requested_path: str,
        *,
        fingerprint: str | None = None,
    ) -> SdkResolution | None:
        try:
            normalized = _safe_relative_posix(
                requested_path, field="requested_path"
            )
        except NativeSdkCatalogError:
            return None
        matches: list[SdkResolution] = []
        for bundle in self.bundles:
            if fingerprint is not None and bundle.fingerprint_sha256 != fingerprint:
                continue
            for item in bundle.files:
                if item.requested_path == normalized:
                    matches.append(
                        SdkResolution(
                            bundle_id=bundle.bundle_id,
                            bundle_fingerprint=bundle.fingerprint_sha256,
                            requested_path=item.requested_path,
                            source_path=item.source_path,
                            sha256=item.sha256,
                        )
                    )
        return matches[0] if len(matches) == 1 else None


def apply_sdk_overlay(
    staging_dir: str | Path,
    resolution: SdkResolution,
) -> AppliedOverlay:
    """Copy one missing SDK file into staging without overwriting player bytes."""

    requested_path = _safe_relative_posix(
        resolution.requested_path, field="requested_path"
    )
    staging = Path(staging_dir)
    staging.mkdir(parents=True, exist_ok=True)
    staging_root = staging.resolve()
    destination = staging / Path(*PurePosixPath(requested_path).parts)
    resolved_destination = destination.resolve(strict=False)
    try:
        resolved_destination.relative_to(staging_root)
    except ValueError as exc:
        raise NativeSdkCatalogError(
            f"SDK overlay destination escapes staging: {requested_path}"
        ) from exc

    if destination.exists() or destination.is_symlink():
        return AppliedOverlay(
            resolution.bundle_id,
            requested_path,
            resolution.source_path,
            destination,
            resolution.sha256,
            False,
        )

    destination.parent.mkdir(parents=True, exist_ok=True)
    resolved_parent = destination.parent.resolve()
    try:
        resolved_parent.relative_to(staging_root)
    except ValueError as exc:
        raise NativeSdkCatalogError(
            f"SDK overlay parent escapes staging: {requested_path}"
        ) from exc

    actual_source_hash = _sha256_file(resolution.source_path)
    if actual_source_hash != resolution.sha256:
        raise NativeSdkCatalogError(
            f"SDK source hash changed after catalog load: {resolution.source_path}"
        )

    copied = False
    try:
        with resolution.source_path.open("rb") as source, destination.open("xb") as target:
            copied = True
            shutil.copyfileobj(source, target)
        actual_destination_hash = _sha256_file(destination)
        if actual_destination_hash != resolution.sha256:
            raise NativeSdkCatalogError(
                f"SDK overlay hash mismatch for destination: {destination}"
            )
    except FileExistsError:
        copied = False
    except Exception:
        if copied:
            destination.unlink(missing_ok=True)
        raise

    return AppliedOverlay(
        resolution.bundle_id,
        requested_path,
        resolution.source_path,
        destination,
        resolution.sha256,
        copied,
    )


def apply_runtime_overlay(
    staging_dir: str | Path,
    requested_path: str,
    source_path: str | Path,
) -> AppliedRuntimeFile:
    """Stage one backend-owned runtime file without overwriting player bytes."""

    normalized = _safe_relative_posix(requested_path, field="requested_path")
    source = Path(source_path).resolve()
    if not source.is_file() or source.is_symlink():
        raise NativeSdkCatalogError(f"runtime source is not a regular file: {source}")
    source_hash = _sha256_file(source)
    staging = Path(staging_dir)
    staging.mkdir(parents=True, exist_ok=True)
    staging_root = staging.resolve()
    destination = staging / Path(*PurePosixPath(normalized).parts)
    resolved_destination = destination.resolve(strict=False)
    try:
        resolved_destination.relative_to(staging_root)
    except ValueError as exc:
        raise NativeSdkCatalogError(
            f"runtime overlay destination escapes staging: {normalized}"
        ) from exc
    if destination.exists() or destination.is_symlink():
        return AppliedRuntimeFile(
            normalized,
            source,
            destination,
            source_hash,
            False,
        )
    destination.parent.mkdir(parents=True, exist_ok=True)
    try:
        destination.parent.resolve().relative_to(staging_root)
    except ValueError as exc:
        raise NativeSdkCatalogError(
            f"runtime overlay parent escapes staging: {normalized}"
        ) from exc
    copied = False
    try:
        with source.open("rb") as source_handle, destination.open("xb") as target:
            copied = True
            shutil.copyfileobj(source_handle, target)
        if _sha256_file(destination) != source_hash:
            raise NativeSdkCatalogError(
                f"runtime overlay hash mismatch for destination: {destination}"
            )
    except FileExistsError:
        copied = False
    except Exception:
        if copied:
            destination.unlink(missing_ok=True)
        raise
    return AppliedRuntimeFile(
        normalized,
        source,
        destination,
        source_hash,
        copied,
    )
