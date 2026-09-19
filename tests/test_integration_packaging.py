"""What a release tag carries: the integration HACS installs, and examples pinned to it."""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

import releve

ROOT = Path(__file__).resolve().parents[1]
# A pre-release (0.4.0rc1) leaves the examples and the changelog on the last final release.
final_release = pytest.mark.skipif(
    re.fullmatch(r"\d+\.\d+\.\d+", releve.__version__) is None, reason="a pre-release"
)


def test_the_integration_carries_releve_s_version() -> None:
    """HACS installs a release tag; the integration it gets must say the same version."""
    manifest = json.loads((ROOT / "custom_components/releve/manifest.json").read_text())
    assert manifest["version"] == releve.__version__


@final_release
def test_the_examples_pin_the_release_they_ship_with() -> None:
    """An example on `:latest` upgrades by surprise; one on an old tag teaches an old releve."""
    for name in ("README.md", "docker-compose.yaml"):
        text = (ROOT / name).read_text(encoding="utf-8")
        tags = set(re.findall(r"ngsanogo/releve[:@](\S+)", text))
        assert tags == {f"v{releve.__version__}"}, name


@final_release
def test_the_changelog_has_an_entry_for_the_release() -> None:
    changelog = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    assert f"## [{releve.__version__}] - " in changelog


def test_the_repository_has_the_layout_hacs_expects() -> None:
    integrations = [p.name for p in (ROOT / "custom_components").iterdir() if p.is_dir()]
    assert integrations == ["releve"]  # HACS takes exactly one integration per repository
    hacs = json.loads((ROOT / "hacs.json").read_text())
    manifest = json.loads((ROOT / "custom_components/releve/manifest.json").read_text())
    assert hacs["name"] == manifest["name"] == manifest["domain"] == "releve"
    assert not manifest["requirements"]  # it only speaks HTTP, through Home Assistant's aiohttp


def test_every_translation_has_the_same_keys() -> None:
    def keys(node: object, prefix: str = "") -> set[str]:
        if not isinstance(node, dict):
            return {prefix}
        return {k for key, value in node.items() for k in keys(value, f"{prefix}/{key}")}

    folder = ROOT / "custom_components/releve"
    reference = keys(json.loads((folder / "strings.json").read_text()))
    for translation in sorted((folder / "translations").glob("*.json")):
        assert keys(json.loads(translation.read_text())) == reference, translation.name
