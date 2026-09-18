"""The Home Assistant integration ships in lockstep with releve, the way HACS wants it."""

from __future__ import annotations

import json
from pathlib import Path

import releve

ROOT = Path(__file__).resolve().parents[1]


def test_the_integration_carries_releve_s_version() -> None:
    """HACS installs a release tag; the integration it gets must say the same version."""
    manifest = json.loads((ROOT / "custom_components/releve/manifest.json").read_text())
    assert manifest["version"] == releve.__version__


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
