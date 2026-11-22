"""Hermetic behavioral tests for ``gardenia host setup``."""

from __future__ import annotations

import json
from pathlib import Path
import tomllib

from conftest import (
    install_healthy_host_shims,
    make_isolated_env,
    make_python_shim,
    run_cli,
)
import pytest

from gardenia.cli import main as cli_main
import gardenia.host as host_mod
from gardenia.host import setup_host
from gardenia.models import GardeniaError


def _install_logging_installer(bin_dir: Path, name: str, log_path: Path) -> None:
    make_python_shim(
        bin_dir,
        name,
        f"""
        import json, sys
        with open({str(log_path)!r}, 'a', encoding='utf-8') as fh:
            fh.write(json.dumps({{'argv': sys.argv}}) + '\\n')
        sys.exit(0)
        """,
    )


def _read_logged_commands(log_path: Path) -> list[list[str]]:
    if not log_path.exists():
        return []
    out: list[list[str]] = []
    for line in log_path.read_text(encoding="utf-8").splitlines():
        argv = json.loads(line)["argv"]
        out.append([Path(argv[0]).name, *argv[1:]])
    return out


def test_host_setup_dry_run_plans_without_mutating(tmp_path: Path) -> None:
    env, home, bin_dir = make_isolated_env(tmp_path)
    log_path = tmp_path / "setup.jsonl"
    _install_logging_installer(bin_dir, "uv", log_path)
    make_python_shim(
        bin_dir,
        "agy",
        f"""
        import json, sys
        if sys.argv[1:] == ["plugin", "list", "--json"]:
            print("[]")
            sys.exit(0)
        with open({str(log_path)!r}, 'a', encoding='utf-8') as fh:
            fh.write(json.dumps({{'argv': sys.argv}}) + '\\n')
        """,
    )

    proc = run_cli(
        [
            "host",
            "setup",
            "--clients",
            "agy",
            "--reserve-pct",
            "5",
            "--host-reserve-credit",
            "20",
            "--min-remaining-credit",
            "5",
            "--dry-run",
            "--json",
        ],
        cwd=tmp_path,
        env=env,
    )
    assert proc.returncode == 0, proc.stderr
    data = json.loads(proc.stdout)
    assert data["dry_run"] is True
    assert data["ok"] is True
    commands = [a["command"] for a in data["actions"] if a["kind"] == "command"]
    assert ["uv", "tool", "install", "howhard"] in commands
    assert ["uv", "tool", "install", "agroute"] in commands
    assert ["uv", "tool", "install", "agcoord"] in commands
    assert ["agc", "host", "install", "--user"] in commands
    assert ["agy", "plugin", "install", "https://github.com/xu-hao/agrein"] in commands
    assert any(a["kind"] == "write_file" and a["component"] == "agroute-config" for a in data["actions"])
    assert not (home / ".config" / "agroute" / "config.toml").exists()
    assert _read_logged_commands(log_path) == []


def test_host_setup_preserves_enforced_cgroup_v2_broker_and_refuses_user_downgrade(tmp_path: Path) -> None:
    env, home, bin_dir = make_isolated_env(tmp_path)
    log_path = tmp_path / "calls.jsonl"
    _install_logging_installer(bin_dir, "uv", log_path)
    install_healthy_host_shims(bin_dir, home, broker_mode="cgroup-v2", log_path=log_path)

    # --broker auto (even with --upgrade) must NEVER call agc host install --user!
    proc_auto = run_cli(["host", "setup", "--broker", "auto", "--upgrade", "--json"], cwd=tmp_path, env=env)
    assert proc_auto.returncode == 0, proc_auto.stderr
    cmds = _read_logged_commands(log_path)
    assert not any(c[:3] == ["agc", "host", "install"] for c in cmds)
    assert any(c == ["uv", "tool", "install", "--upgrade", "howhard"] for c in cmds)
    assert any(c == ["agy", "plugin", "uninstall", "howhard"] for c in cmds)
    assert any(c == ["claude", "plugin", "update", "howhard@howhard", "--scope", "user"] for c in cmds)
    assert any(c == ["codex", "plugin", "marketplace", "upgrade", "howhard"] for c in cmds)

    # --broker user must refuse with exit 2 BEFORE executing any mutations!
    log_path.unlink()
    proc_refuse = run_cli(["host", "setup", "--broker", "user", "--upgrade", "--json"], cwd=tmp_path, env=env)
    assert proc_refuse.returncode == 2
    refused_payload = json.loads(proc_refuse.stdout)
    assert refused_payload["ok"] is False
    assert "cgroup-v2" in refused_payload["error"]
    # Confirm uv was never called because refusal happened before mutation.
    post_refusal_cmds = _read_logged_commands(log_path)
    assert not any(c[0] == "uv" for c in post_refusal_cmds)

    # --broker enforced --upgrade runs agc host upgrade --download.
    log_path.unlink()
    proc_enf = run_cli(["host", "setup", "--broker", "enforced", "--upgrade", "--clients", "agy", "--json"], cwd=tmp_path, env=env)
    assert proc_enf.returncode == 0, proc_enf.stderr
    enf_cmds = _read_logged_commands(log_path)
    assert ["agc", "host", "upgrade", "--download"] in enf_cmds


def test_host_setup_pipx_fallback_and_reserve_config_overwrite_safety(tmp_path: Path) -> None:
    env, home, bin_dir = make_isolated_env(tmp_path)
    log_path = tmp_path / "calls.jsonl"
    _install_logging_installer(bin_dir, "pipx", log_path)
    install_healthy_host_shims(bin_dir, home, broker_mode="unreachable", log_path=log_path)
    # Remove howhard so pipx install howhard is triggered.
    (bin_dir / "howhard").unlink()

    proc = run_cli(
        [
            "host",
            "setup",
            "--clients",
            "agy",
            "--reserve-pct",
            "5",
            "--host-reserve-credit",
            "20",
            "--min-remaining-credit",
            "5",
            "--json",
        ],
        cwd=tmp_path,
        env=env,
    )
    assert proc.returncode == 0, proc.stderr
    data = json.loads(proc.stdout)
    assert data["installer"] == "pipx"
    cmds = _read_logged_commands(log_path)
    assert ["pipx", "install", "howhard"] in cmds
    assert ["agc", "host", "install", "--user"] in cmds

    cfg_path = home / ".config" / "agroute" / "config.toml"
    assert cfg_path.is_file()
    parsed = tomllib.loads(cfg_path.read_text(encoding="utf-8"))
    for agent in ("codex", "claude", "agy"):
        assert parsed["agents"][agent]["host_reserve_pct"] == 5
        assert parsed["agents"][agent]["host_reserve_credit_usd"] == 20
        assert parsed["agents"][agent]["min_remaining_credit_usd"] == 5

    # Refuses to overwrite existing config without --overwrite-config.
    proc_refuse = run_cli(
        ["host", "setup", "--clients", "agy", "--reserve-pct", "10", "--json"],
        cwd=tmp_path,
        env=env,
    )
    assert proc_refuse.returncode == 2
    assert tomllib.loads(cfg_path.read_text(encoding="utf-8"))["agents"]["agy"]["host_reserve_pct"] == 5

    # Overwrites cleanly with --overwrite-config.
    proc_overwrite = run_cli(
        ["host", "setup", "--clients", "agy", "--reserve-pct", "10", "--overwrite-config", "--json"],
        cwd=tmp_path,
        env=env,
    )
    assert proc_overwrite.returncode == 0, proc_overwrite.stderr
    assert tomllib.loads(cfg_path.read_text(encoding="utf-8"))["agents"]["agy"]["host_reserve_pct"] == 10


def test_host_setup_refuses_broker_mutation_on_probe_timeout_unless_skip_or_force(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    env, home, bin_dir = make_isolated_env(tmp_path)
    log_path = tmp_path / "calls.jsonl"
    _install_logging_installer(bin_dir, "uv", log_path)
    install_healthy_host_shims(bin_dir, home, broker_mode="timeout", log_path=log_path)

    monkeypatch.setattr(host_mod, "BROKER_PROBE_TIMEOUT_SECONDS", 0.05)
    monkeypatch.setattr("os.environ", dict(env))

    for mode in ("auto", "user", "enforced"):
        if log_path.exists():
            log_path.unlink()
        with pytest.raises(GardeniaError) as exc_info:
            setup_host(
                broker=mode,
                clients=["agy"],
                upgrade=True,
                reserve_pct=5.0,
                env=env,
                platform="linux",
            )
        assert exc_info.value.exit_code == 2
        assert "timed out" in exc_info.value.message
        assert "--broker skip" in exc_info.value.message
        assert "--force-broker" in exc_info.value.message

        # Confirm refusal happened before executing any CLI/plugin/broker/config mutations.
        logged = _read_logged_commands(log_path)
        assert not any(c[0] == "uv" for c in logged)
        assert not any(c[:2] == ["agc", "host"] for c in logged)
        assert not any(c[:2] == ["agy", "plugin"] and c[2] != "list" for c in logged)
        assert not (home / ".config" / "agroute" / "config.toml").exists()

        # Also verify CLI boundary exit code and JSON error envelope.
        rc = cli_main(["host", "setup", "--broker", mode, "--clients", "agy", "--json"])
        out = capsys.readouterr().out
        assert rc == 2
        err_payload = json.loads(out)
        assert err_payload["ok"] is False
        assert err_payload["exit_code"] == 2
        assert "timed out" in err_payload["error"]

    # --broker skip succeeds cleanly even when the broker probe times out.
    if log_path.exists():
        log_path.unlink()
    res_skip = setup_host(
        broker="skip",
        clients=["agy"],
        upgrade=True,
        env=env,
        platform="linux",
    )
    assert res_skip["ok"] is True
    broker_actions = [a for a in res_skip["actions"] if a["component"] == "agcoord-broker"]
    assert len(broker_actions) == 1
    assert broker_actions[0]["kind"] == "skip"
    assert broker_actions[0]["status"] == "skipped"
    skip_cmds = _read_logged_commands(log_path)
    assert not any(c[:2] == ["agc", "host"] for c in skip_cmds)
    assert ["uv", "tool", "install", "--upgrade", "howhard"] in skip_cmds

    # --force-broker explicitly overrides the timeout refusal across broker modes.
    rc_force_auto = cli_main(
        ["host", "setup", "--broker", "auto", "--force-broker", "--clients", "agy", "--dry-run", "--json"]
    )
    force_auto_payload = json.loads(capsys.readouterr().out)
    assert rc_force_auto == 0
    force_auto_cmds = [a["command"] for a in force_auto_payload["actions"] if a["kind"] == "command"]
    assert ["agc", "host", "install", "--user"] in force_auto_cmds

    if log_path.exists():
        log_path.unlink()
    res_force_user = setup_host(
        broker="user",
        force_broker=True,
        clients=["agy"],
        env=env,
        platform="linux",
    )
    assert res_force_user["ok"] is True
    assert ["agc", "host", "install", "--user"] in _read_logged_commands(log_path)

    res_force_enf = setup_host(
        broker="enforced",
        force_broker=True,
        clients=["agy"],
        dry_run=True,
        env=env,
        platform="linux",
    )
    assert res_force_enf["ok"] is True
    force_enf_cmds = [a["command"] for a in res_force_enf["actions"] if a["kind"] == "command"]
    assert ["agc", "host", "install", "--download"] in force_enf_cmds
