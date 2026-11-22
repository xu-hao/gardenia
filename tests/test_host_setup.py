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
