"""Hermetic behavioral tests for unified ``gardenia check``, CLI metadata, and artwork/dogfood contracts."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import re

from conftest import (
    init_git_repo,
    install_healthy_host_shims,
    make_isolated_env,
    run_cli,
)
from gardenia import __version__
from gardenia.repo import check_repo


def test_cli_version(tmp_path: Path) -> None:
    env, _, _ = make_isolated_env(tmp_path)
    proc = run_cli(["--version"], cwd=tmp_path, env=env)
    assert proc.returncode == 0
    assert proc.stdout.strip() == f"gardenia {__version__}"


def test_unified_check_tags_host_and_repo_scopes_explicitly(tmp_path: Path) -> None:
    env, home, bin_dir = make_isolated_env(tmp_path)
    install_healthy_host_shims(
        bin_dir,
        home,
        agy_plugins=[
            {"name": "agrein", "enabled": True, "version": "0.1.0"},
            {"name": "agcoord", "enabled": True, "version": "0.9.1"},  # drift warning
            {"name": "howhard", "enabled": True, "version": "4.0.0"},
            {"name": "agroute", "enabled": True, "version": "0.6.0"},
        ],
    )
    repo = init_git_repo(tmp_path / "repo", env)  # missing AGENTS.md -> repo error

    proc = run_cli(["check", "--repo", str(repo), "--json"], cwd=tmp_path, env=env)
    assert proc.returncode == 1
    data = json.loads(proc.stdout)
    assert data["format"] == "gardenia-check-v1"
    assert data["ok"] is False
    assert data["host"] is not None
    assert data["repo"] is not None

    host_findings = [f for f in data["findings"] if f["scope"] == "host"]
    repo_findings = [f for f in data["findings"] if f["scope"] == "repo"]
    assert any(f["component"] == "agy:agcoord" and f["severity"] == "warning" for f in host_findings)
    assert any(f["component"] == "agents-md" and f["severity"] == "error" for f in repo_findings)
    for finding in data["findings"]:
        assert set(finding.keys()) == {"scope", "severity", "component", "message", "remediation"}


def test_unified_check_outside_git_repo_skips_repo_cleanly(tmp_path: Path) -> None:
    env, home, bin_dir = make_isolated_env(tmp_path)
    install_healthy_host_shims(bin_dir, home)

    non_git = tmp_path / "not-a-repo"
    non_git.mkdir()
    proc = run_cli(["check", "--strict", "--json"], cwd=non_git, env=env)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    data = json.loads(proc.stdout)
    assert data["ok"] is True
    assert data["repo"] is None
    assert data["findings"] == []

    # Explicitly passing a non-Git --repo refuses with exit 2.
    bad = run_cli(["check", "--repo", str(non_git), "--json"], cwd=non_git, env=env)
    assert bad.returncode == 2


def test_gardenia_svg_monochrome_and_provenance_checksum() -> None:
    repo_root = Path(__file__).resolve().parents[1]
    svg_path = repo_root / "docs" / "assets" / "gardenia.svg"
    readme_path = repo_root / "docs" / "assets" / "README.md"
    svg_text = svg_path.read_text(encoding="utf-8")

    assert 'viewBox="0 0 512 512"' in svg_text
    assert "<text" not in svg_text
    assert "gradient" not in svg_text.lower()
    colors = set(re.findall(r"#[0-9a-fA-F]{3,8}", svg_text))
    assert colors == {"#000000", "#ffffff"}

    digest = hashlib.sha256(svg_path.read_bytes()).hexdigest()
    assert digest in readme_path.read_text(encoding="utf-8")


def test_repository_dogfoods_gardenia_cleanly(tmp_path: Path) -> None:
    env, _, _ = make_isolated_env(tmp_path)
    repo_root = Path(__file__).resolve().parents[1]
    result = check_repo(repo_root, strict=True, env=env)
    assert result["ok"] is True, result["findings"]
    assert result["repo"]["tracked"]["config"]["source"] == "gardenia.toml"
    assert result["repo"]["tracked"]["agents_md"]["in_sync"] is True
    assert result["repo"]["tracked"]["claude_md"]["status"] == "symlink_to_agents"
    non_info = [f for f in result["findings"] if f["severity"] != "info"]
    assert non_info == []
