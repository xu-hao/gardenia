"""Hermetic test fixtures for gardenia (never touches real HOME or live host state)."""

from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import textwrap
from typing import Any


def write_executable(path: Path, content: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return path


def make_isolated_env(tmp_path: Path) -> tuple[dict[str, str], Path, Path]:
    """Build a strictly isolated (env, home_dir, bin_dir) with zero host tool leakage."""

    home = tmp_path / "home"
    home.mkdir(parents=True, exist_ok=True)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(parents=True, exist_ok=True)

    # Expose only git inside bin_dir so real howhard/agr/agc/agy/claude/codex/uv/pipx never leak.
    git_real = shutil.which("git")
    assert git_real is not None, "git must be installed on the test machine"
    git_link = bin_dir / "git"
    if not git_link.exists():
        git_link.symlink_to(git_real)

    env = {
        "HOME": str(home),
        "XDG_CONFIG_HOME": str(home / ".config"),
        "XDG_STATE_HOME": str(home / ".local" / "state"),
        "PATH": str(bin_dir),
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": str(home / ".gitconfig"),
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
    }
    return env, home, bin_dir


def make_python_shim(bin_dir: Path, name: str, body: str, *, log_path: Path | None = None) -> Path:
    header = f"#!{sys.executable}\n"
    if log_path is not None:
        header += (
            "import json as _json, sys as _sys\n"
            f"with open({str(log_path)!r}, 'a', encoding='utf-8') as _fh:\n"
            "    _fh.write(_json.dumps({'argv': _sys.argv}) + '\\n')\n"
        )
    script = header + textwrap.dedent(body).lstrip()
    return write_executable(bin_dir / name, script)


def install_healthy_host_shims(
    bin_dir: Path,
    home: Path,
    *,
    howhard_ver: str = "4.0.0",
    agr_ver: str = "0.6.0",
    agc_ver: str = "0.10.1",
    broker_mode: str = "cgroup-v2",
    agy_plugins: list[dict[str, Any]] | None = None,
    claude_plugins: list[dict[str, Any]] | None = None,
    codex_versions: dict[str, str] | None = None,
    codex_enabled: dict[str, bool] | None = None,
    log_path: Path | None = None,
) -> None:
    """Populate fake host CLI shims and Codex config/cache inside tmp_path."""

    make_python_shim(
        bin_dir,
        "howhard",
        f"""
        import sys
        if "--version" in sys.argv:
            print("howhard {howhard_ver}")
            sys.exit(0)
        sys.exit(0)
        """,
        log_path=log_path,
    )

    make_python_shim(
        bin_dir,
        "agr",
        f"""
        import json, sys
        if "--version" in sys.argv:
            print("agr {agr_ver}")
            sys.exit(0)
        if sys.argv[1:4] == ["prices", "pending", "--json"]:
            print(json.dumps({{"format": "agroute-price-pending-v1", "pending_count": 0, "price_request": None}}))
            sys.exit(0)
        sys.exit(0)
        """,
        log_path=log_path,
    )

    if broker_mode == "cgroup-v2":
        agc_list_payload: dict[str, Any] = {
            "protocol": 5,
            "broker_pid": 12345,
            "capacities": {"cpu": 8, "jobs": 8, "memory": 17179869184},
            "resource_bindings": {
                "cpu": {"backend": "cgroup-v2", "kind": "cpu", "mode": "required", "unit": "logical-cpu"},
                "memory": {"backend": "cgroup-v2", "kind": "memory", "mode": "required", "unit": "bytes"},
            },
            "active": [],
            "queued": [],
        }
    elif broker_mode == "admission-only":
        agc_list_payload = {
            "protocol": 5,
            "broker_pid": 12345,
            "capacities": {"cpu": 8, "jobs": 8},
            "resource_bindings": {
                "jobs": {"backend": None, "kind": "generic", "mode": "admission-only", "unit": "admission-unit"},
            },
            "active": [],
            "queued": [],
        }
    else:
        agc_list_payload = {}

    if broker_mode in ("cgroup-v2", "admission-only"):
        agc_body = f"""
        import sys
        if "--version" in sys.argv:
            print("agc {agc_ver}")
            sys.exit(0)
        if sys.argv[1:3] == ["--json", "list"]:
            rest = sys.argv[3:]
            if rest:
                if len(rest) == 2 and rest[0] == "--limit":
                    try:
                        limit_val = int(rest[1])
                    except ValueError:
                        limit_val = 0
                    if limit_val <= 0:
                        print("error: limit must be a positive integer", file=sys.stderr)
                        sys.exit(2)
                else:
                    print(f"unexpected args: {{rest}}", file=sys.stderr)
                    sys.exit(2)
            print({json.dumps(json.dumps(agc_list_payload))})
            sys.exit(0)
        sys.exit(0)
        """
    elif broker_mode == "timeout":
        agc_body = f"""
        import sys, time
        if "--version" in sys.argv:
            print("agc {agc_ver}")
            sys.exit(0)
        if sys.argv[1:3] == ["--json", "list"]:
            time.sleep(0.5)
            sys.exit(0)
        sys.exit(0)
        """
    else:
        agc_body = f"""
        import sys
        if "--version" in sys.argv:
            print("agc {agc_ver}")
            sys.exit(0)
        if sys.argv[1:3] == ["--json", "list"]:
            print("broker unreachable", file=sys.stderr)
            sys.exit(1)
        sys.exit(0)
        """

    make_python_shim(bin_dir, "agc", agc_body, log_path=log_path)

    default_agy = agy_plugins if agy_plugins is not None else [
        {"name": "agrein", "enabled": True, "version": "0.1.0"},
        {"name": "agcoord", "enabled": True, "version": agc_ver},
        {"name": "howhard", "enabled": True, "version": howhard_ver},
        {"name": "agroute", "enabled": True, "version": agr_ver},
    ]
    make_python_shim(
        bin_dir,
        "agy",
        f"""
        import sys
        if sys.argv[1:] == ["plugin", "list", "--json"]:
            print({json.dumps(json.dumps(default_agy))})
            sys.exit(0)
        sys.exit(0)
        """,
        log_path=log_path,
    )

    default_claude = claude_plugins if claude_plugins is not None else [
        {"id": "agrein@agrein", "version": "0.1.0", "scope": "user", "enabled": True},
        {"id": "agcoord@agcoord", "version": agc_ver, "scope": "user", "enabled": True},
        {"id": "howhard@howhard", "version": howhard_ver, "scope": "user", "enabled": True},
        {"id": "agroute@agroute", "version": agr_ver, "scope": "user", "enabled": True},
    ]
    make_python_shim(
        bin_dir,
        "claude",
        f"""
        import sys
        if sys.argv[1:] == ["plugin", "list", "--json"]:
            print({json.dumps(json.dumps(default_claude))})
            sys.exit(0)
        sys.exit(0)
        """,
        log_path=log_path,
    )

    # Codex shim must NEVER be called with "plugin list" (which opens an interactive TUI).
    make_python_shim(
        bin_dir,
        "codex",
        """
        import sys
        if sys.argv[1:3] == ["plugin", "list"]:
            print("ERROR: codex plugin list must never be invoked!", file=sys.stderr)
            sys.exit(99)
        sys.exit(0)
        """,
        log_path=log_path,
    )

    c_versions = codex_versions if codex_versions is not None else {
        "agrein": "0.1.0",
        "agcoord": agc_ver,
        "howhard": howhard_ver,
        "agroute": agr_ver,
    }
    c_enabled = codex_enabled if codex_enabled is not None else {pkg: True for pkg in c_versions}

    codex_dir = home / ".codex"
    codex_dir.mkdir(parents=True, exist_ok=True)
    toml_lines: list[str] = []
    for pkg in c_versions:
        toml_lines.extend(
            [
                f"[marketplaces.{pkg}]",
                'source_type = "git"',
                f'source = "https://github.com/xu-hao/{pkg}.git"',
                "",
            ]
        )
    for pkg, enabled in c_enabled.items():
        toml_lines.extend(
            [
                f'[plugins."{pkg}@{pkg}"]',
                f"enabled = {'true' if enabled else 'false'}",
                "",
            ]
        )
    (codex_dir / "config.toml").write_text("\n".join(toml_lines), encoding="utf-8")

    for pkg, ver in c_versions.items():
        manifest = codex_dir / "plugins" / "cache" / pkg / pkg / ver / "plugin.json"
        manifest.parent.mkdir(parents=True, exist_ok=True)
        manifest.write_text(json.dumps({"name": pkg, "version": ver}), encoding="utf-8")


def init_git_repo(repo_dir: Path, env: dict[str, str]) -> Path:
    repo_dir.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-b", "main"], cwd=repo_dir, env=env, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Test User"], cwd=repo_dir, env=env, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=repo_dir, env=env, check=True)
    (repo_dir / "README.md").write_text("# Sample repo\n", encoding="utf-8")
    subprocess.run(["git", "add", "README.md"], cwd=repo_dir, env=env, check=True)
    subprocess.run(["git", "commit", "-m", "Initial commit"], cwd=repo_dir, env=env, check=True, capture_output=True)
    return repo_dir


def run_cli(
    args: list[str],
    *,
    cwd: Path,
    env: dict[str, str],
) -> subprocess.CompletedProcess[str]:
    run_env = dict(env)
    src_dir = str(Path(__file__).resolve().parents[1] / "src")
    run_env["PYTHONPATH"] = src_dir
    return subprocess.run(
        [sys.executable, "-m", "gardenia", *args],
        cwd=cwd,
        env=run_env,
        capture_output=True,
        text=True,
        check=False,
    )
