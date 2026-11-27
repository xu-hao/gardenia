"""Hermetic behavioral tests for ``gardenia host check``."""

from __future__ import annotations

import json
from pathlib import Path

from conftest import (
    install_healthy_host_shims,
    make_isolated_env,
    make_python_shim,
    run_cli,
)
import pytest

from gardenia.cli import main as cli_main
import gardenia.host as host_mod
from gardenia.host import check_host


def test_host_check_clean_enforced_linux_host(tmp_path: Path) -> None:
    env, home, bin_dir = make_isolated_env(tmp_path)
    install_healthy_host_shims(bin_dir, home, broker_mode="cgroup-v2")

    cfg_path = home / ".config" / "agroute" / "config.toml"
    cfg_path.parent.mkdir(parents=True, exist_ok=True)
    cfg_path.write_text(
        '[agents.agy]\nplan = "ultra-200"\nhost_reserve_pct = 5.0\nhost_reserve_credit_usd = 20.0\nmin_remaining_credit_usd = 5.0\nmax_session_used_pct = 90.0\nmax_weekly_used_pct = 95.0\n',
        encoding="utf-8",
    )

    proc = run_cli(["host", "check", "--strict", "--json"], cwd=tmp_path, env=env)
    assert proc.returncode == 0, proc.stderr
    data = json.loads(proc.stdout)
    assert data["format"] == "gardenia-check-v1"
    assert data["ok"] is True
    assert data["findings"] == []
    assert data["host"]["clis"]["howhard"]["min_version"] == "6.0.0"
    assert data["host"]["clis"]["howhard"]["version"] == "6.0.0"
    assert data["host"]["clis"]["howhard"]["status"] == "ok"
    assert data["host"]["clis"]["agroute"]["min_version"] == "0.7.0"
    assert data["host"]["clis"]["agroute"]["version"] == "0.7.0"
    assert data["host"]["clis"]["agroute"]["status"] == "ok"
    assert data["host"]["broker"]["mode"] == "cgroup-v2"
    assert data["host"]["broker"]["enforced"] is True
    assert data["host"]["agroute_config"]["valid"] is True
    assert data["host"]["agroute_config"]["agents"]["agy"]["host_reserve_credit_usd"] == 20.0
    assert data["host"]["agroute_config"]["agents"]["agy"]["plan"] == "ultra-200"


def test_host_check_never_invokes_codex_plugin_list_and_picks_highest_semver(tmp_path: Path) -> None:
    env, home, bin_dir = make_isolated_env(tmp_path)
    log_file = tmp_path / "calls.jsonl"
    install_healthy_host_shims(bin_dir, home, agr_ver="0.10.0", log_path=log_file)

    # Add multiple cached semvers for agroute (lexicographical '0.9.1' > '0.10.0' must lose to semver 0.10.0).
    for ver in ("0.5.2", "0.9.1", "0.10.0"):
        manifest = home / ".codex" / "plugins" / "cache" / "agroute" / "agroute" / ver / "plugin.json"
        manifest.parent.mkdir(parents=True, exist_ok=True)
        manifest.write_text(json.dumps({"name": "agroute", "version": ver}), encoding="utf-8")

    # Remove howhard cache to exercise fallback to ~/.codex/.tmp/marketplaces/howhard/plugin.json.
    hh_cache = home / ".codex" / "plugins" / "cache" / "howhard"
    for p in sorted(hh_cache.rglob("*"), reverse=True):
        if p.is_file():
            p.unlink()
        else:
            p.rmdir()
    hh_cache.rmdir()
    tmp_manifest = home / ".codex" / ".tmp" / "marketplaces" / "howhard" / "plugin.json"
    tmp_manifest.parent.mkdir(parents=True, exist_ok=True)
    tmp_manifest.write_text(json.dumps({"name": "howhard", "version": "6.0.0"}), encoding="utf-8")

    proc = run_cli(["host", "check", "--clients", "codex", "--strict", "--json"], cwd=tmp_path, env=env)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    data = json.loads(proc.stdout)
    codex_plugins = data["host"]["clients"]["codex"]["plugins"]
    assert codex_plugins["agroute"]["version"] == "0.10.0"
    assert codex_plugins["howhard"]["version"] == "6.0.0"

    # Verify codex binary was never invoked at all during host check.
    if log_file.exists():
        calls = [json.loads(line)["argv"] for line in log_file.read_text(encoding="utf-8").splitlines()]
        assert not any(Path(argv[0]).name == "codex" for argv in calls)


def test_host_check_detects_cli_vs_plugin_version_drift_and_strict_mode(tmp_path: Path) -> None:
    env, home, bin_dir = make_isolated_env(tmp_path)
    install_healthy_host_shims(
        bin_dir,
        home,
        howhard_ver="6.0.0",
        agr_ver="0.7.0",
        agc_ver="0.10.1",
        agy_plugins=[
            {"name": "agrein", "enabled": True, "version": "0.1.0"},
            {"name": "agcoord", "enabled": True, "version": "0.9.1"},
            {"name": "howhard", "enabled": True, "version": "6.0.0"},
            {"name": "agroute", "enabled": True, "version": "0.7.0"},
        ],
        codex_versions={
            "agrein": "0.1.0",
            "agcoord": "0.10.1",
            "howhard": "6.0.0",
            "agroute": "0.5.2",
        },
    )

    non_strict = run_cli(["host", "check", "--json"], cwd=tmp_path, env=env)
    assert non_strict.returncode == 0
    payload = json.loads(non_strict.stdout)
    warnings = [f for f in payload["findings"] if f["severity"] == "warning"]
    assert len(warnings) == 2
    assert all(f["scope"] == "host" for f in warnings)
    components = {f["component"] for f in warnings}
    assert components == {"agy:agcoord", "codex:agroute"}
    assert any("agy plugin uninstall agcoord" in f["remediation"] for f in warnings)
    assert any("codex plugin marketplace upgrade agroute" in f["remediation"] for f in warnings)

    strict = run_cli(["host", "check", "--strict", "--json"], cwd=tmp_path, env=env)
    assert strict.returncode == 1
    assert json.loads(strict.stdout)["ok"] is False


def test_host_check_reports_missing_and_outdated_clis_and_invalid_agroute_config(tmp_path: Path) -> None:
    env, home, bin_dir = make_isolated_env(tmp_path)
    # Only install outdated howhard (5.0.0 < 6.0.0); omit agr and agc.
    make_python_shim(
        bin_dir,
        "howhard",
        """
        import sys
        print("howhard 5.0.0")
        """,
    )
    bad_cfg = home / ".config" / "agroute" / "config.toml"
    bad_cfg.parent.mkdir(parents=True, exist_ok=True)
    bad_cfg.write_text("[agents.agy\nbroken toml\n", encoding="utf-8")

    proc = run_cli(["host", "check", "--json"], cwd=tmp_path, env=env)
    assert proc.returncode == 1
    data = json.loads(proc.stdout)
    assert data["ok"] is False
    assert data["host"]["clis"]["howhard"]["min_version"] == "6.0.0"
    assert data["host"]["clis"]["howhard"]["version"] == "5.0.0"
    assert data["host"]["clis"]["howhard"]["status"] == "outdated"
    assert any(
        f["component"] == "howhard" and "version 5.0.0 is below minimum 6.0.0" in f["message"]
        for f in data["findings"]
    )
    assert data["host"]["clis"]["agroute"]["status"] == "missing"
    assert data["host"]["clis"]["agcoord"]["status"] == "missing"
    assert data["host"]["agroute_config"]["valid"] is False
    assert all(f["scope"] == "host" for f in data["findings"])


@pytest.mark.parametrize(
    ("shim_kwarg", "component", "installed_version", "min_version"),
    [
        ("howhard_ver", "howhard", "5.0.0", "6.0.0"),
        ("agr_ver", "agroute", "0.6.9", "0.7.0"),
    ],
)
def test_host_check_rejects_cli_just_below_required_minimum(
    tmp_path: Path,
    shim_kwarg: str,
    component: str,
    installed_version: str,
    min_version: str,
) -> None:
    env, home, bin_dir = make_isolated_env(tmp_path)
    # Every other CLI and plugin is healthy, so the single old CLI alone must fail the check.
    install_healthy_host_shims(bin_dir, home, **{shim_kwarg: installed_version})

    proc = run_cli(["host", "check", "--json"], cwd=tmp_path, env=env)
    assert proc.returncode == 1, proc.stdout + proc.stderr
    data = json.loads(proc.stdout)
    assert data["ok"] is False
    cli = data["host"]["clis"][component]
    assert cli["min_version"] == min_version
    assert cli["version"] == installed_version
    assert cli["status"] == "outdated"
    assert cli["reason"] == "below-min-version"
    errors = [f for f in data["findings"] if f["severity"] == "error"]
    assert [f["component"] for f in errors] == [component]
    assert f"version {installed_version} is below minimum {min_version}" in errors[0]["message"]


def test_host_check_skips_agcoord_on_non_linux_unless_requested(tmp_path: Path) -> None:
    env, home, bin_dir = make_isolated_env(tmp_path)
    install_healthy_host_shims(bin_dir, home)
    (bin_dir / "agc").unlink()

    mac = check_host(env=env, platform="darwin", strict=True)
    assert mac["ok"] is True
    assert mac["host"]["clis"]["agcoord"]["status"] == "skipped"
    assert mac["host"]["clis"]["agcoord"]["reason"] == "platform-unsupported"
    assert mac["host"]["broker"]["status"] == "skipped"

    mac_forced = check_host(env=env, platform="darwin", with_agcoord=True)
    assert mac_forced["ok"] is False
    assert mac_forced["host"]["clis"]["agcoord"]["status"] == "missing"


def test_host_check_admission_only_broker_is_info_not_error(tmp_path: Path) -> None:
    env, home, bin_dir = make_isolated_env(tmp_path)
    install_healthy_host_shims(bin_dir, home, broker_mode="admission-only")

    proc = run_cli(["host", "check", "--strict", "--json"], cwd=tmp_path, env=env)
    assert proc.returncode == 0
    data = json.loads(proc.stdout)
    assert data["host"]["broker"]["mode"] == "admission-only"
    assert data["host"]["broker"]["enforced"] is False
    assert any(f["component"] == "agcoord-broker" and f["severity"] == "info" for f in data["findings"])


def test_host_check_bounds_agc_broker_probe_with_limit_1_and_30s_timeout(tmp_path: Path) -> None:
    env, home, bin_dir = make_isolated_env(tmp_path)
    log_file = tmp_path / "calls.jsonl"
    install_healthy_host_shims(bin_dir, home, broker_mode="cgroup-v2", log_path=log_file)

    proc = run_cli(["host", "check", "--strict", "--json"], cwd=tmp_path, env=env)
    assert proc.returncode == 0, proc.stdout + proc.stderr

    calls = [
        [Path(entry["argv"][0]).name, *entry["argv"][1:]]
        for line in log_file.read_text(encoding="utf-8").splitlines()
        for entry in (json.loads(line),)
    ]
    agc_calls = [c for c in calls if c[0] == "agc"]
    assert ["agc", "--json", "list", "--limit", "1"] in agc_calls
    assert ["agc", "--json", "list"] not in agc_calls
    assert host_mod.BROKER_PROBE_TIMEOUT_SECONDS == 30.0


def test_host_check_broker_probe_timeout_emits_non_destructive_remediation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    env, home, bin_dir = make_isolated_env(tmp_path / "timeout_case")
    log_file = tmp_path / "timeout_calls.jsonl"
    install_healthy_host_shims(bin_dir, home, broker_mode="timeout", log_path=log_file)

    monkeypatch.setattr(host_mod, "BROKER_PROBE_TIMEOUT_SECONDS", 0.05)
    monkeypatch.setattr("os.environ", dict(env))

    rc = cli_main(["host", "check", "--json"])
    captured = capsys.readouterr()
    assert rc == 1
    data = json.loads(captured.out)
    assert data["ok"] is False
    assert data["host"]["broker"]["status"] == "unreachable"
    assert data["host"]["broker"]["reason"] == "timeout"
    assert data["host"]["broker"]["reachable"] is False

    broker_findings = [f for f in data["findings"] if f["component"] == "agcoord-broker"]
    assert len(broker_findings) == 1
    finding = broker_findings[0]
    assert finding["scope"] == "host"
    assert finding["severity"] == "error"
    assert "timed out" in finding["message"]
    assert "agc --json list --limit 1" in finding["message"]
    assert finding["remediation"] == "agc --json list --limit 1"
    assert "agc host install" not in finding["remediation"]

    calls = [
        [Path(entry["argv"][0]).name, *entry["argv"][1:]]
        for line in log_file.read_text(encoding="utf-8").splitlines()
        for entry in (json.loads(line),)
    ]
    assert ["agc", "--json", "list", "--limit", "1"] in calls

    # Non-timeout failure (e.g. missing/stopped broker) still recommends installing a broker.
    env_unreach, home_unreach, bin_unreach = make_isolated_env(tmp_path / "unreach_case")
    install_healthy_host_shims(bin_unreach, home_unreach, broker_mode="unreachable")
    res_unreach = check_host(env=env_unreach, platform="linux")
    assert res_unreach["host"]["broker"]["status"] == "unreachable"
    assert res_unreach["host"]["broker"]["reason"] == "command-failed"
    unreach_findings = [f for f in res_unreach["findings"] if f["component"] == "agcoord-broker"]
    assert len(unreach_findings) == 1
    assert "agc host install --user" in unreach_findings[0]["remediation"]
