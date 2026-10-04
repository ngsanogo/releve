"""One number per tool: every other place that names it is held to its source.

Dependabot moves one file at a time. Without these, a Dockerfile bumped alone
would ship an interpreter the tests never ran on, and nothing would say so.
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]


def read(name: str) -> str:
    return (ROOT / name).read_text(encoding="utf-8")


def test_the_image_runs_the_python_the_tests_run_on() -> None:
    """.python-version is what uv installs here and in CI; the image must not differ."""
    python = read(".python-version").strip()
    assert re.fullmatch(r"\d+\.\d+\.\d+", python), "an exact version, not a floating minor"
    assert re.findall(r"\bpython:([\w.]+)-slim", read("Dockerfile")) == [python]


def test_uv_is_one_version_everywhere() -> None:
    """mise installs it for development, setup-uv in CI, the Dockerfile in the image."""
    uv = tomllib.loads(read("mise.toml"))["tools"]["uv"]
    pyproject = tomllib.loads(read("pyproject.toml"))
    assert pyproject["build-system"]["requires"] == [f"uv_build=={uv}"]
    assert re.findall(r"astral-sh/uv:([\w.]+)@sha256:", read("Dockerfile")) == [uv]

    # Without `version`, setup-uv installs the latest uv: every step must name it.
    steps = [
        (workflow.name, step)
        for workflow in sorted((ROOT / ".github/workflows").glob("*.yml"))
        for job in yaml.safe_load(workflow.read_text(encoding="utf-8"))["jobs"].values()
        for step in job.get("steps", [])
        if step.get("uses", "").startswith("astral-sh/setup-uv@")
    ]
    assert steps, "no setup-uv step found: the workflows changed shape"
    for workflow_name, step in steps:
        assert step.get("with", {}).get("version") == uv, workflow_name


def test_uv_is_not_required_at_an_exact_version() -> None:
    """Dependabot's uv updater runs its own uv; under `required-version` uv refuses to run."""
    assert "required-version" not in tomllib.loads(read("pyproject.toml")).get("tool", {}).get(
        "uv", {}
    )


def test_the_commit_hook_and_ci_scan_with_the_same_gitleaks() -> None:
    """CI and `make secrets` take gitleaks from mise.toml; pre-commit names its own."""
    gitleaks = tomllib.loads(read("mise.toml"))["tools"]["gitleaks"]
    hook = re.search(r"gitleaks/gitleaks\n\s+rev: (\S+)", read(".pre-commit-config.yaml"))
    assert hook is not None
    assert hook.group(1) == f"v{gitleaks}"


def test_ci_and_make_test_start_the_same_mqtt_broker() -> None:
    image = re.compile(r"eclipse-mosquitto:\S+")
    makefile = image.findall(read("Makefile"))
    assert len(makefile) == 1
    assert "@sha256:" in makefile[0], "pinned by digest"
    assert image.findall(read(".github/workflows/ci.yml")) == makefile
