"""Audit the actual compressed release, including hidden and unranked packages."""

import csv
import hashlib
import io
import json
import tarfile
from pathlib import Path
import pytest

ROOT = Path(__file__).resolve().parents[1]
GAMES = json.loads((ROOT / "configs/paper.json").read_text())["games"]


@pytest.mark.parametrize("game", GAMES)
def test_archive_contains_exactly_permitted_players(game):
    ratings = json.loads((ROOT / f"results/elo/{game}/measured_elo.json").read_text())
    ratings = ratings.get("ratings") if isinstance(ratings, dict) else ratings
    expected = {
        r["player_id"]: rank for rank, r in enumerate(ratings, 1) if rank > 8 and rank % 2 == 0
    }
    archive = ROOT / f"assets/{game}.tar.gz"
    row = next(
        x
        for x in json.loads((ROOT / "assets/manifest.json").read_text())["games"]
        if x["game"] == game
    )
    assert hashlib.sha256(archive.read_bytes()).hexdigest() == row["sha256"]
    with tarfile.open(archive) as t:
        prefix = f"games/{game}/players/pool/"
        actual = set()
        seen = set()
        manifest = json.load(t.extractfile(f"assets/manifests/{game}.json"))
        files = {r["path"]: r for r in manifest["files"]}
        for member in t:
            assert member.isfile() and not member.issym() and not member.islnk()
            assert member.name not in seen
            seen.add(member.name)
            if member.name.startswith(prefix):
                actual.add(member.name[len(prefix) :].split("/")[0])
            if member.name in files:
                b = t.extractfile(member).read()
                assert hashlib.sha256(b).hexdigest() == files[member.name]["sha256"]
        assert actual == set(expected)
        provenance = json.load(t.extractfile(f"games/{game}/players/publication.json"))
        assert {x["player_id"]: x["rank"] for x in provenance["published_players"]} == expected
        rows = list(
            csv.DictReader(
                io.StringIO(t.extractfile(f"games/{game}/players/manifest.tsv").read().decode()),
                delimiter="\t",
            )
        )
        assert {r["player_id"] for r in rows} == set(expected)
        assert seen == set(files) | {f"assets/manifests/{game}.json"}


def test_no_runtime_secrets_or_full_pool_unpacking_in_release():
    assert (
        json.loads((ROOT / "configs/distribution.json").read_text())["formal_evaluation"]
        == "remote-required"
    )
    assert "我们只公布未进入决赛圈的偶数人类选手的代码" in (ROOT / "README.md").read_text()


def test_installer_rejects_extra_private_players(tmp_path):
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "asset_installer", ROOT / "scripts/install_assets.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    player = tmp_path / "games/pacman/players"
    (player / "pool/allowed").mkdir(parents=True)
    (player / "pool/withheld").mkdir()
    publication = player / "publication.json"
    body = json.dumps({"published_players": [{"player_id": "allowed", "rank": 10}]}).encode()
    publication.write_bytes(body)
    manifest = {
        "files": [
            {
                "path": publication.relative_to(tmp_path).as_posix(),
                "size": len(body),
                "sha256": hashlib.sha256(body).hexdigest(),
            }
        ]
    }
    with pytest.raises(ValueError, match="non-public"):
        module.verify_game(tmp_path, manifest)
    assert (player / "pool/withheld").exists()
