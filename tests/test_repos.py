"""Hermetic behavioral tests for ``gardenia repos`` registry, check, and isolated-worktree upgrade sweep."""

from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tomllib

from conftest import (
    init_git_repo,
    make_isolated_env,
    make_python_shim,
    run_cli,
    write_executable,
)
from gardenia.models import MANAGED_BEGIN, MANAGED_END


def _ensure_bash_in_bin(bin_dir: Path) -> None:
    bash_real = shutil.which("bash")
    assert bash_real is not None, "bash must be installed on the test machine"
    link = bin_dir / "bash"
    if not link.exists():
        link.symlink_to(bash_real)


def _git(repo: Path, env: dict[str, str], *args: str) -> str:
    res = subprocess.run(
        ["git", *args],
        cwd=repo,
        env=env,
        check=True,
        capture_output=True,
        text=True,
    )
    return res.stdout


def _init_repo_with_bare_origin(
    tmp_path: Path,
    name: str,
    env: dict[str, str],
    *,
    gate: str = "bash scripts/check",
    gate_body: str = "#!/usr/bin/env bash\nset -euo pipefail\necho 'check ok'\n",
    agcoord: str = "auto",
    docs_exemption: bool = True,
    extra_files: dict[str, str] | None = None,
    stale_agents: bool = False,
) -> tuple[Path, Path]:
    """Create a bare origin remote and a primary clone initialized with gardenia."""

    bare = tmp_path / "remotes" / f"{name}.git"
    bare.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "--bare", "-b", "main", str(bare)], env=env, check=True, capture_output=True)

    repo = init_git_repo(tmp_path / "workspace" / name, env)
    write_executable(repo / "scripts" / "check", gate_body)
    if extra_files:
        for rel, content in extra_files.items():
            target = repo / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")

    # Initialize tracked gardenia config & AGENTS.md without touching registry yet.
    init_proc = run_cli(
        [
            "repo",
            "init",
            "--repo",
            str(repo),
            "--gate",
            gate,
            "--agcoord",
            agcoord,
            *(["--docs-exemption"] if docs_exemption else ["--no-docs-exemption"]),
            "--save-config",
            "--no-register",
            "--json",
        ],
        cwd=tmp_path,
        env=env,
    )
    assert init_proc.returncode == 0, init_proc.stdout + init_proc.stderr

    if stale_agents:
        (repo / "AGENTS.md").write_text(
            f"# {name}\n\n{MANAGED_BEGIN}\nstale content\n{MANAGED_END}\n",
            encoding="utf-8",
        )

    _git(repo, env, "add", "-A")
    _git(repo, env, "commit", "-m", "Configure repo for gardenia")
    _git(repo, env, "remote", "add", "origin", str(bare))
    _git(repo, env, "push", "-u", "origin", "main")
    _git(repo, env, "remote", "set-head", "origin", "main")
    return repo, bare


def _install_agc_shim(bin_dir: Path, log_path: Path) -> None:
    """Install a fake agc shim supporting broker probe, agc run, and agc land."""

    make_python_shim(
        bin_dir,
        "agc",
        """
        import json, os, subprocess, sys
        argv = sys.argv[1:]
        if argv[:2] == ["--json", "list"]:
            print(json.dumps({
                "protocol": 5,
                "broker_pid": 4242,
                "capacities": {"cpu": 8, "jobs": 8, "memory": 17179869184},
                "resource_bindings": {
                    "cpu": {"backend": "cgroup-v2", "kind": "cpu", "mode": "required", "unit": "logical-cpu"},
                    "memory": {"backend": "cgroup-v2", "kind": "memory", "mode": "required", "unit": "bytes"},
                },
                "active": [],
                "queued": [],
            }))
            sys.exit(0)
        if argv and argv[0] in ("run", "land"):
            if "--" not in argv:
                print("missing -- separator", file=sys.stderr)
                sys.exit(2)
            sep = argv.index("--")
            pre_sep = argv[:sep]
            label_val = None
            if "--label" in pre_sep:
                idx = pre_sep.index("--label")
                if idx + 1 < len(pre_sep):
                    label_val = pre_sep[idx + 1]
            else:
                for arg in pre_sep:
                    if arg.startswith("--label="):
                        label_val = arg.split("=", 1)[1]
                        break
            if label_val is None or not label_val.strip():
                print(f"agc {argv[0]} requires a non-empty --label before --", file=sys.stderr)
                sys.exit(2)
            cmd = argv[sep + 1 :]
            proc = subprocess.run(cmd, env=os.environ)
            sys.exit(proc.returncode)
        sys.exit(0)
        """,
        log_path=log_path,
    )


def _install_gh_shim(bin_dir: Path, state_path: Path, log_path: Path) -> None:
    """Install a fake gh shim tracking issues and PRs in a JSON state file."""

    make_python_shim(
        bin_dir,
        "gh",
        f"""
        import json, pathlib, sys
        state_file = pathlib.Path({str(state_path)!r})
        if state_file.exists():
            state = json.loads(state_file.read_text(encoding="utf-8"))
        else:
            state = {{"next_issue": 10, "next_pr": 100, "issues": [], "prs": [], "merged": []}}

        def save():
            state_file.parent.mkdir(parents=True, exist_ok=True)
            state_file.write_text(json.dumps(state), encoding="utf-8")

        def get_opt(args, flag, default=""):
            if flag in args:
                idx = args.index(flag)
                if idx + 1 < len(args):
                    return args[idx + 1]
            return default

        argv = sys.argv[1:]
        if argv[:2] == ["issue", "list"]:
            open_issues = [i for i in state["issues"] if i.get("state", "open") == "open"]
            print(json.dumps(open_issues))
            sys.exit(0)
        if argv[:2] == ["issue", "create"]:
            num = state["next_issue"]
            state["next_issue"] += 1
            title = get_opt(argv, "--title")
            body = get_opt(argv, "--body")
            url = f"https://github.com/example/repo/issues/{{num}}"
            state["issues"].append({{"number": num, "title": title, "body": body, "url": url, "state": "open"}})
            save()
            print(url)
            sys.exit(0)
        if argv[:2] == ["pr", "create"]:
            num = state["next_pr"]
            state["next_pr"] += 1
            title = get_opt(argv, "--title")
            body = get_opt(argv, "--body")
            head = get_opt(argv, "--head")
            url = f"https://github.com/example/repo/pull/{{num}}"
            state["prs"].append({{"number": num, "title": title, "body": body, "head": head, "url": url}})
            save()
            print(url)
            sys.exit(0)
        if argv[:2] == ["pr", "merge"]:
            ref = argv[2] if len(argv) > 2 else ""
            state["merged"].append({{"ref": ref, "args": argv[3:]}})
            save()
            print(f"Merged PR {{ref}}")
            sys.exit(0)
        print(f"unexpected gh args: {{argv}}", file=sys.stderr)
        sys.exit(2)
        """,
        log_path=log_path,
    )


def _read_jsonl(path: Path) -> list[dict]:
    if not path.is_file():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def test_repo_init_registry_auto_register_no_register_dry_run_and_linked_worktree(tmp_path: Path) -> None:
    env, home, _ = make_isolated_env(tmp_path)
    reg_file = home / ".config" / "gardenia" / "repos.toml"
    repo = init_git_repo(tmp_path / "alpha", env)

    # 1. --dry-run plans registration without writing repos.toml.
    dry = run_cli(["repo", "init", "--repo", str(repo), "--dry-run", "--json"], cwd=tmp_path, env=env)
    assert dry.returncode == 0, dry.stderr
    dry_payload = json.loads(dry.stdout)
    reg_actions = [a for a in dry_payload["actions"] if a["component"] == "repos-registry"]
    assert len(reg_actions) == 1
    assert reg_actions[0]["status"] == "planned"
    assert not reg_file.exists()

    # 2. --no-register skips registration cleanly.
    no_reg = run_cli(["repo", "init", "--repo", str(repo), "--no-register", "--json"], cwd=tmp_path, env=env)
    assert no_reg.returncode == 0, no_reg.stderr
    no_reg_payload = json.loads(no_reg.stdout)
    reg_actions = [a for a in no_reg_payload["actions"] if a["component"] == "repos-registry"]
    assert len(reg_actions) == 1
    assert reg_actions[0]["status"] == "skipped"
    assert not reg_file.exists()

    # 3. Default repo init on primary checkout auto-registers repo root.
    real = run_cli(["repo", "init", "--repo", str(repo), "--json"], cwd=tmp_path, env=env)
    assert real.returncode == 0, real.stderr
    assert reg_file.is_file()
    reg_data = tomllib.loads(reg_file.read_text(encoding="utf-8"))
    assert reg_data == {"format": "gardenia-repos-v1", "repos": [str(repo.resolve())]}

    # 4. Re-running repo init skips registry update (already registered).
    again = run_cli(["repo", "init", "--repo", str(repo), "--json"], cwd=tmp_path, env=env)
    assert again.returncode == 0
    again_actions = [a for a in json.loads(again.stdout)["actions"] if a["component"] == "repos-registry"]
    assert again_actions[0]["status"] == "skipped"

    # 5. Linked worktree auto-skips registration under --allow-worktree and refuses explicit --register.
    wt = tmp_path / "alpha-wt"
    _git(repo, env, "worktree", "add", "-b", "issue-99", str(wt), "main")
    try:
        refused = run_cli(
            ["repo", "init", "--repo", str(wt), "--allow-worktree", "--register", "--json"],
            cwd=tmp_path,
            env=env,
        )
        assert refused.returncode == 2
        assert not (wt / "AGENTS.md").exists()

        allowed = run_cli(
            ["repo", "init", "--repo", str(wt), "--allow-worktree", "--json"],
            cwd=tmp_path,
            env=env,
        )
        assert allowed.returncode == 0, allowed.stderr
        wt_actions = [a for a in json.loads(allowed.stdout)["actions"] if a["component"] == "repos-registry"]
        assert wt_actions[0]["status"] == "skipped"
        reg_after = tomllib.loads(reg_file.read_text(encoding="utf-8"))
        assert reg_after["repos"] == [str(repo.resolve())]
    finally:
        subprocess.run(["git", "worktree", "remove", "--force", str(wt)], cwd=repo, env=env, check=False)


def test_repos_registry_list_add_remove_scan_prune_and_validation(tmp_path: Path) -> None:
    env, _, _ = make_isolated_env(tmp_path)
    custom_reg = tmp_path / "custom-config" / "my-repos.toml"
    env["GARDENIA_REPOS_CONFIG"] = str(custom_reg)

    # Empty / non-existent registry lists cleanly with exit 0.
    empty = run_cli(["repos", "list", "--json"], cwd=tmp_path, env=env)
    assert empty.returncode == 0, empty.stderr
    empty_data = json.loads(empty.stdout)
    assert empty_data["format"] == "gardenia-repos-list-v1"
    assert empty_data["registry_exists"] is False
    assert empty_data["repos"] == []

    # Build a workspace containing multiple managed and excluded directories.
    ws = tmp_path / "ws"
    repo_a = init_git_repo(ws / "repo-a", env)
    run_cli(["repo", "init", "--repo", str(repo_a), "--save-config", "--no-register"], cwd=tmp_path, env=env)

    repo_b = init_git_repo(ws / "nested" / "repo-b", env)
    (repo_b / "pyproject.toml").write_text('[tool.gardenia]\ngate = "pytest -q"\n', encoding="utf-8")

    repo_c = init_git_repo(ws / "repo-c", env)
    (repo_c / "AGENTS.md").write_text(f"# C\n\n{MANAGED_BEGIN}\nstale\n{MANAGED_END}\n", encoding="utf-8")

    repo_unmanaged = init_git_repo(ws / "unmanaged", env)
    hidden_repo = init_git_repo(ws / ".hidden" / "secret-repo", env)
    (hidden_repo / "gardenia.toml").write_text('gate = "true"\n', encoding="utf-8")
    nm_repo = init_git_repo(ws / "node_modules" / "dep-repo", env)
    (nm_repo / "gardenia.toml").write_text('gate = "true"\n', encoding="utf-8")
    pc_repo = init_git_repo(ws / "__pycache__" / "cache-repo", env)
    (pc_repo / "gardenia.toml").write_text('gate = "true"\n', encoding="utf-8")

    wt_in_ws = ws / "repo-a-wt"
    _git(repo_a, env, "worktree", "add", "-b", "wt-branch", str(wt_in_ws), "main")

    try:
        # Invalid --max-depth rejected with exit 2.
        bad_depth = run_cli(["repos", "scan", "--root", str(ws), "--max-depth", "-1", "--json"], cwd=tmp_path, env=env)
        assert bad_depth.returncode == 2

        # --dry-run scan discovers only primary managed repos, respecting depth & exclusions.
        scan_dry = run_cli(["repos", "scan", "--root", str(ws), "--dry-run", "--json"], cwd=tmp_path, env=env)
        assert scan_dry.returncode == 0, scan_dry.stderr
        dry_data = json.loads(scan_dry.stdout)
        expected_discovered = sorted([str(repo_a.resolve()), str(repo_b.resolve()), str(repo_c.resolve())])
        assert dry_data["discovered"] == expected_discovered
        assert not custom_reg.exists()

        # Real scan writes canonical sorted repos.toml.
        scan_real = run_cli(["repos", "scan", "--root", str(ws), "--json"], cwd=tmp_path, env=env)
        assert scan_real.returncode == 0, scan_real.stderr
        assert custom_reg.is_file()
        assert json.loads(scan_real.stdout)["repos"] == expected_discovered

        # repos add refuses non-git directory and linked worktree with exit 2.
        non_git = tmp_path / "plain"
        non_git.mkdir()
        assert run_cli(["repos", "add", str(non_git), "--json"], cwd=tmp_path, env=env).returncode == 2
        assert run_cli(["repos", "add", str(wt_in_ws), "--json"], cwd=tmp_path, env=env).returncode == 2

        # repos add with no args defaults to cwd (resolving subdirectory to repo root).
        sub = repo_unmanaged / "subdir"
        sub.mkdir()
        add_cwd = run_cli(["repos", "add", "--json"], cwd=sub, env=env)
        assert add_cwd.returncode == 0, add_cwd.stderr
        add_data = json.loads(add_cwd.stdout)
        assert str(repo_unmanaged.resolve()) in add_data["added"]
        assert str(repo_unmanaged.resolve()) in add_data["repos"]

        # repos list reports fields accurately; --strict fails because repo-c is out of sync & unmanaged is unmanaged.
        lst = run_cli(["repos", "list", "--json"], cwd=tmp_path, env=env)
        assert lst.returncode == 0
        lst_data = json.loads(lst.stdout)
        by_name = {item["name"]: item for item in lst_data["repos"]}
        assert by_name["repo-a"]["registered"] is True
        assert by_name["repo-a"]["managed"] is True
        assert by_name["repo-a"]["agents_md_in_sync"] is True
        assert by_name["repo-a"]["branch"] == "main"
        assert by_name["repo-a"]["clean"] is False  # AGENTS.md/gardenia.toml uncommitted
        assert by_name["repo-c"]["agents_md_in_sync"] is False
        assert by_name["unmanaged"]["managed"] is False

        lst_strict = run_cli(["repos", "list", "--strict", "--json"], cwd=tmp_path, env=env)
        assert lst_strict.returncode == 1
        assert json.loads(lst_strict.stdout)["ok"] is False

        # repos remove removes paths idempotently.
        rm = run_cli(
            ["repos", "remove", str(repo_unmanaged), str(tmp_path / "never-existed"), "--json"],
            cwd=tmp_path,
            env=env,
        )
        assert rm.returncode == 0
        rm_data = json.loads(rm.stdout)
        assert rm_data["removed"] == [str(repo_unmanaged.resolve())]
        assert rm_data["not_found"] == [str((tmp_path / "never-existed").resolve())]

        # Manually insert a missing path and a linked worktree into repos.toml, then verify prune.
        missing_path = str((tmp_path / "vanished-repo").resolve())
        custom_reg.write_text(
            'format = "gardenia-repos-v1"\n'
            f'repos = [\n  {json.dumps(str(repo_a.resolve()))},\n  {json.dumps(missing_path)},\n  {json.dumps(str(wt_in_ws.resolve()))},\n]\n',
            encoding="utf-8",
        )
        prune_dry = run_cli(["repos", "prune", "--dry-run", "--json"], cwd=tmp_path, env=env)
        assert prune_dry.returncode == 0
        pd = json.loads(prune_dry.stdout)
        assert sorted(pd["removed"]) == sorted([missing_path, str(wt_in_ws.resolve())])
        assert len(tomllib.loads(custom_reg.read_text(encoding="utf-8"))["repos"]) == 3

        prune_real = run_cli(["repos", "prune", "--json"], cwd=tmp_path, env=env)
        assert prune_real.returncode == 0
        assert json.loads(prune_real.stdout)["repos"] == [str(repo_a.resolve())]

        # Invalid repos.toml cases all return exit 2.
        for bad_content in (
            "[broken toml\n",
            'format = "wrong-v0"\nrepos = []\n',
            'format = "gardenia-repos-v1"\nrepos = []\nextra = 1\n',
            'format = "gardenia-repos-v1"\nrepos = "not-a-list"\n',
            'format = "gardenia-repos-v1"\nrepos = [""]\n',
        ):
            custom_reg.write_text(bad_content, encoding="utf-8")
            bad = run_cli(["repos", "list", "--json"], cwd=tmp_path, env=env)
            assert bad.returncode == 2, f"Expected exit 2 for: {bad_content!r}"
    finally:
        subprocess.run(["git", "worktree", "remove", "--force", str(wt_in_ws)], cwd=repo_a, env=env, check=False)


def test_repos_check_fleet_healthy_drifted_broken_and_empty(tmp_path: Path) -> None:
    env, home, _ = make_isolated_env(tmp_path)

    # Empty target list exits 2.
    empty = run_cli(["repos", "check", "--json"], cwd=tmp_path, env=env)
    assert empty.returncode == 2

    healthy = init_git_repo(tmp_path / "healthy", env)
    run_cli(["repo", "init", "--repo", str(healthy), "--save-config"], cwd=tmp_path, env=env)

    drifted = init_git_repo(tmp_path / "drifted", env)
    run_cli(["repo", "init", "--repo", str(drifted), "--save-config"], cwd=tmp_path, env=env)
    (drifted / "AGENTS.md").write_text(f"# Drifted\n\n{MANAGED_BEGIN}\noutdated\n{MANAGED_END}\n", encoding="utf-8")

    # Healthy + drifted: non-strict exits 0 (warning only), --strict exits 1.
    non_strict = run_cli(["repos", "check", "--json"], cwd=tmp_path, env=env)
    assert non_strict.returncode == 0, non_strict.stdout + non_strict.stderr
    ns_data = json.loads(non_strict.stdout)
    assert ns_data["format"] == "gardenia-repos-check-v1"
    assert ns_data["ok"] is True
    assert ns_data["summary"]["total"] == 2

    strict = run_cli(["repos", "check", "--strict", "--json"], cwd=tmp_path, env=env)
    assert strict.returncode == 1
    s_data = json.loads(strict.stdout)
    assert s_data["ok"] is False
    assert s_data["summary"]["failed"] == 1
    assert s_data["summary"]["ok"] == 1

    # Inject a broken missing registered path: fleet check does NOT abort; records repo-level error entry.
    reg_file = home / ".config" / "gardenia" / "repos.toml"
    missing_path = str((tmp_path / "missing-repo").resolve())
    reg_file.write_text(
        'format = "gardenia-repos-v1"\n'
        f'repos = [\n  {json.dumps(str(healthy.resolve()))},\n  {json.dumps(missing_path)},\n]\n',
        encoding="utf-8",
    )
    mixed = run_cli(["repos", "check", "--json"], cwd=tmp_path, env=env)
    assert mixed.returncode == 1
    mixed_data = json.loads(mixed.stdout)
    assert mixed_data["summary"]["total"] == 2
    assert mixed_data["summary"]["ok"] == 1
    assert mixed_data["summary"]["failed"] == 1
    by_path = {r["path"]: r for r in mixed_data["repos"]}
    assert by_path[str(healthy.resolve())]["ok"] is True
    assert by_path[missing_path]["ok"] is False
    assert by_path[missing_path]["error"]

    # Explicit --repo targets only the specified repo.
    explicit = run_cli(["repos", "check", "--repo", str(healthy), "--strict", "--json"], cwd=tmp_path, env=env)
    assert explicit.returncode == 0
    assert json.loads(explicit.stdout)["summary"]["total"] == 1


def test_repos_upgrade_validation_dry_run_and_up_to_date_keeps_primary_untouched(tmp_path: Path) -> None:
    env, _, bin_dir = make_isolated_env(tmp_path)
    _ensure_bash_in_bin(bin_dir)

    repo = init_git_repo(tmp_path / "clean-local", env)
    write_executable(repo / "scripts" / "check", "#!/usr/bin/env bash\nset -euo pipefail\necho 'ok'\n")
    run_cli(["repo", "init", "--repo", str(repo), "--agcoord", "none", "--save-config"], cwd=tmp_path, env=env)
    _git(repo, env, "add", "-A")
    _git(repo, env, "commit", "-m", "Add gardenia policy")

    # Add an uncommitted marker file in primary checkout to verify primary checkout is NEVER mutated.
    dirty_file = repo / "local-uncommitted.txt"
    dirty_file.write_text("do not touch\n", encoding="utf-8")

    # Invalid --package values refused with exit 2.
    for bad_pkg in ("", "foo bar", "pkg;rm -rf /", "$(id)", "--flag"):
        bad = run_cli(["repos", "upgrade", "--repo", str(repo), "--package", bad_pkg, "--json"], cwd=tmp_path, env=env)
        assert bad.returncode == 2, f"Expected exit 2 for --package {bad_pkg!r}"

    wt_root = tmp_path / "wt-root"

    # --dry-run creates no worktrees and exits 0.
    dry = run_cli(
        ["repos", "upgrade", "--repo", str(repo), "--worktree-root", str(wt_root), "--dry-run", "--json"],
        cwd=tmp_path,
        env=env,
    )
    assert dry.returncode == 0, dry.stdout + dry.stderr
    dry_data = json.loads(dry.stdout)
    assert dry_data["format"] == "gardenia-repos-upgrade-v1"
    assert dry_data["dry_run"] is True
    assert dry_data["repos"][0]["status"] == "planned"
    assert not wt_root.exists()

    # Real run on up-to-date local repo (no origin remote) succeeds and cleans up completely.
    run = run_cli(
        ["repos", "upgrade", "--repo", str(repo), "--worktree-root", str(wt_root), "--json"],
        cwd=tmp_path,
        env=env,
    )
    assert run.returncode == 0, run.stdout + run.stderr
    data = json.loads(run.stdout)
    assert data["ok"] is True
    assert data["repos"][0]["status"] == "up_to_date"
    assert data["repos"][0]["base_ref"] == "HEAD"
    assert data["repos"][0]["used_agcoord"] is False

    # Primary checkout untouched and zero leftover temporary worktrees.
    assert dirty_file.read_text(encoding="utf-8") == "do not touch\n"
    assert list(wt_root.iterdir()) == []
    wt_list = _git(repo, env, "worktree", "list", "--porcelain")
    assert wt_list.count("worktree ") == 1


def test_repos_upgrade_changes_verified_and_land_with_agc_and_docs_exemption(tmp_path: Path) -> None:
    env, _, bin_dir = make_isolated_env(tmp_path)
    _ensure_bash_in_bin(bin_dir)

    agc_log = tmp_path / "agc.jsonl"
    gh_log = tmp_path / "gh.jsonl"
    gh_state = tmp_path / "gh-state.json"
    uv_log = tmp_path / "uv.jsonl"

    _install_agc_shim(bin_dir, agc_log)
    _install_gh_shim(bin_dir, gh_state, gh_log)
    make_python_shim(
        bin_dir,
        "uv",
        """
        import pathlib, sys
        if sys.argv[1:2] == ["lock"]:
            lock_file = pathlib.Path("uv.lock")
            lock_file.write_text(lock_file.read_text(encoding="utf-8") + "# upgraded\\n", encoding="utf-8")
            sys.exit(0)
        sys.exit(0)
        """,
        log_path=uv_log,
    )

    # Repo A: docs-only update (drifted AGENTS.md, no lockfiles, docs_exemption = true).
    repo_docs, bare_docs = _init_repo_with_bare_origin(
        tmp_path,
        "repo-docs",
        env,
        agcoord="auto",
        docs_exemption=True,
        stale_agents=True,
    )
    # Repo B: uv.lock + drifted AGENTS.md (must land via agc land, not docs exemption).
    repo_lock, bare_lock = _init_repo_with_bare_origin(
        tmp_path,
        "repo-lock",
        env,
        agcoord="auto",
        docs_exemption=True,
        extra_files={"uv.lock": "version = 1\n"},
        stale_agents=True,
    )

    docs_agents_before = (repo_docs / "AGENTS.md").read_text(encoding="utf-8")
    lock_before = (repo_lock / "uv.lock").read_text(encoding="utf-8")
    wt_root = tmp_path / "wt-root"

    # 1. Without --land: reports changes_verified and leaves primary checkouts untouched.
    verify_proc = run_cli(
        [
            "repos",
            "upgrade",
            "--repo",
            str(repo_docs),
            "--repo",
            str(repo_lock),
            "--package",
            "howhard",
            "--worktree-root",
            str(wt_root),
            "--json",
        ],
        cwd=tmp_path,
        env=env,
    )
    assert verify_proc.returncode == 0, verify_proc.stdout + verify_proc.stderr
    v_data = json.loads(verify_proc.stdout)
    assert v_data["ok"] is True
    v_by_name = {r["name"]: r for r in v_data["repos"]}
    assert v_by_name["repo-docs"]["status"] == "changes_verified"
    assert v_by_name["repo-docs"]["changed_files"] == ["AGENTS.md"]
    assert v_by_name["repo-docs"]["used_agcoord"] is True
    assert v_by_name["repo-lock"]["status"] == "changes_verified"
    assert sorted(v_by_name["repo-lock"]["changed_files"]) == ["AGENTS.md", "uv.lock"]

    # Primary checkouts completely untouched!
    assert (repo_docs / "AGENTS.md").read_text(encoding="utf-8") == docs_agents_before
    assert (repo_lock / "uv.lock").read_text(encoding="utf-8") == lock_before
    assert list(wt_root.iterdir()) == []

    # Verify uv lock --upgrade-package howhard was called.
    uv_calls = _read_jsonl(uv_log)
    assert any(c["argv"][1:] == ["lock", "--upgrade-package", "howhard"] for c in uv_calls)

    # Verify agc run received explicit human-readable --label for each repository.
    verify_agc_calls = _read_jsonl(agc_log)
    verify_run_calls = [c["argv"] for c in verify_agc_calls if len(c["argv"]) > 1 and c["argv"][1] == "run"]
    assert len(verify_run_calls) == 2
    assert verify_run_calls[0][1:4] == ["run", "--label", "upgrade gate (repo-docs)"]
    assert verify_run_calls[1][1:4] == ["run", "--label", "upgrade gate (repo-lock)"]

    # Clear logs before testing --land.
    agc_log.unlink(missing_ok=True)
    gh_log.unlink(missing_ok=True)

    # 2. With --land: repo-docs merges via gh pr merge (docs exemption), repo-lock lands via agc land.
    land_proc = run_cli(
        [
            "repos",
            "upgrade",
            "--repo",
            str(repo_docs),
            "--repo",
            str(repo_lock),
            "--package",
            "howhard",
            "--land",
            "--worktree-root",
            str(wt_root),
            "--json",
        ],
        cwd=tmp_path,
        env=env,
    )
    assert land_proc.returncode == 0, land_proc.stdout + land_proc.stderr
    l_data = json.loads(land_proc.stdout)
    assert l_data["ok"] is True
    l_by_name = {r["name"]: r for r in l_data["repos"]}

    assert l_by_name["repo-docs"]["status"] == "landed"
    assert l_by_name["repo-docs"]["land_method"] == "gh_pr_merge_docs_exemption"
    assert l_by_name["repo-docs"]["pr_url"]
    assert l_by_name["repo-docs"]["issue_url"]

    assert l_by_name["repo-lock"]["status"] == "landed"
    assert l_by_name["repo-lock"]["land_method"] == "agc_land"
    assert l_by_name["repo-lock"]["pr_url"]
    assert l_by_name["repo-lock"]["issue_url"]

    agc_calls = _read_jsonl(agc_log)
    agc_land_calls = [c["argv"] for c in agc_calls if len(c["argv"]) > 1 and c["argv"][1] == "land"]
    assert len(agc_land_calls) == 1
    lock_pr_ref = str(l_by_name["repo-lock"]["pr_number"])
    assert agc_land_calls[0][1:5] == [
        "land",
        lock_pr_ref,
        "--label",
        f"land #{lock_pr_ref} upgrade sweep (repo-lock)",
    ]
    assert "--no-overflow" in agc_land_calls[0]
    assert "memory=4294967296" in agc_land_calls[0]

    gh_calls = _read_jsonl(gh_log)
    gh_merge_calls = [c["argv"] for c in gh_calls if c["argv"][1:3] == ["pr", "merge"]]
    assert len(gh_merge_calls) == 1
    assert str(l_by_name["repo-docs"]["pr_number"]) in gh_merge_calls[0]
    assert "--delete-branch" in gh_merge_calls[0]

    # Branches were pushed to both bare origins; primary checkouts remain untouched with no leftover local branches.
    assert "gardenia-upgrade-" in _git(bare_docs, env, "branch")
    assert "gardenia-upgrade-" in _git(bare_lock, env, "branch")
    assert "gardenia-upgrade-" not in _git(repo_docs, env, "branch")
    assert "gardenia-upgrade-" not in _git(repo_lock, env, "branch")
    assert (repo_docs / "AGENTS.md").read_text(encoding="utf-8") == docs_agents_before
    assert (repo_lock / "uv.lock").read_text(encoding="utf-8") == lock_before
    assert list(wt_root.iterdir()) == []


def test_repos_upgrade_cargo_and_pyproject_venv_paths(tmp_path: Path) -> None:
    env, _, bin_dir = make_isolated_env(tmp_path)
    _ensure_bash_in_bin(bin_dir)

    cargo_log = tmp_path / "cargo.jsonl"
    uv_log = tmp_path / "uv.jsonl"
    make_python_shim(
        bin_dir,
        "cargo",
        """
        import pathlib, sys
        if sys.argv[1:2] == ["update"]:
            lock = pathlib.Path("Cargo.lock")
            lock.write_text(lock.read_text(encoding="utf-8") + "# updated\\n", encoding="utf-8")
            sys.exit(0)
        sys.exit(0)
        """,
        log_path=cargo_log,
    )
    make_python_shim(
        bin_dir,
        "uv",
        """
        import pathlib, stat, sys
        if sys.argv[1:2] == ["venv"]:
            venv_targets = [arg for arg in sys.argv[2:] if not arg.startswith("-")]
            venv_dir = pathlib.Path(venv_targets[0])
            bin_dir = venv_dir / "bin"
            bin_dir.mkdir(parents=True, exist_ok=True)
            marker = bin_dir / "venv-tool"
            marker.write_text("#!/usr/bin/env bash\\necho venv-active\\n", encoding="utf-8")
            marker.chmod(marker.stat().st_mode | stat.S_IXUSR)
            py = bin_dir / "python"
            py.symlink_to(sys.executable)
            sys.exit(0)
        if sys.argv[1:3] == ["pip", "install"]:
            sys.exit(0)
        sys.exit(0)
        """,
        log_path=uv_log,
    )

    cargo_repo = init_git_repo(tmp_path / "rust-repo", env)
    write_executable(cargo_repo / "scripts" / "check", "#!/usr/bin/env bash\nset -euo pipefail\necho ok\n")
    (cargo_repo / "Cargo.lock").write_text("version = 3\n", encoding="utf-8")
    run_cli(["repo", "init", "--repo", str(cargo_repo), "--agcoord", "none", "--save-config", "--no-register"], cwd=tmp_path, env=env)
    _git(cargo_repo, env, "add", "-A")
    _git(cargo_repo, env, "commit", "-m", "Init rust repo")

    py_repo = init_git_repo(tmp_path / "py-repo", env)
    write_executable(
        py_repo / "scripts" / "check",
        "#!/usr/bin/env bash\nset -euo pipefail\nout=\"$(venv-tool)\"\ntest \"$out\" = \"venv-active\"\n",
    )
    (py_repo / "pyproject.toml").write_text(
        '[project]\nname = "demo"\nversion = "0.1.0"\n\n[project.optional-dependencies]\ntest = ["pytest"]\n',
        encoding="utf-8",
    )
    run_cli(["repo", "init", "--repo", str(py_repo), "--agcoord", "none", "--save-config", "--no-register"], cwd=tmp_path, env=env)
    _git(py_repo, env, "add", "-A")
    _git(py_repo, env, "commit", "-m", "Init py repo")

    wt_root = tmp_path / "wt-root"
    proc = run_cli(
        [
            "repos",
            "upgrade",
            "--repo",
            str(cargo_repo),
            "--repo",
            str(py_repo),
            "--package",
            "serde",
            "--worktree-root",
            str(wt_root),
            "--json",
        ],
        cwd=tmp_path,
        env=env,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    by_name = {r["name"]: r for r in json.loads(proc.stdout)["repos"]}
    assert by_name["rust-repo"]["status"] == "changes_verified"
    assert by_name["rust-repo"]["changed_files"] == ["Cargo.lock"]
    assert by_name["py-repo"]["status"] == "up_to_date"

    cargo_calls = _read_jsonl(cargo_log)
    assert any(c["argv"][1:] == ["update", "-p", "serde"] for c in cargo_calls)
    uv_calls = _read_jsonl(uv_log)
    assert any(c["argv"][1:2] == ["venv"] for c in uv_calls)
    assert any(c["argv"][1:3] == ["pip", "install"] and ".[test]" in c["argv"] for c in uv_calls)
    assert any(c["argv"][1:3] == ["pip", "install"] and "--upgrade" in c["argv"] and "serde" in c["argv"] for c in uv_calls)
    assert list(wt_root.iterdir()) == []


def test_repos_upgrade_gate_failed_saves_isolated_log_and_deduplicates_issues(tmp_path: Path) -> None:
    env, home, bin_dir = make_isolated_env(tmp_path)
    _ensure_bash_in_bin(bin_dir)

    gh_log = tmp_path / "gh.jsonl"
    gh_state = tmp_path / "gh-state.json"
    _install_gh_shim(bin_dir, gh_state, gh_log)

    failing_repo, _ = _init_repo_with_bare_origin(
        tmp_path,
        "broken-repo",
        env,
        gate_body="#!/usr/bin/env bash\nset -euo pipefail\necho 'FAIL: regression in dep X' >&2\nexit 1\n",
        agcoord="none",
    )

    wt_root = tmp_path / "wt-root"

    # First run: fails gate, saves log under isolated XDG_STATE_HOME, opens new issue.
    first = run_cli(
        [
            "repos",
            "upgrade",
            "--repo",
            str(failing_repo),
            "--package",
            "pytest",
            "--file-issues",
            "--worktree-root",
            str(wt_root),
            "--json",
        ],
        cwd=tmp_path,
        env=env,
    )
    assert first.returncode == 1
    first_data = json.loads(first.stdout)
    assert first_data["ok"] is False
    entry1 = first_data["repos"][0]
    assert entry1["status"] == "gate_failed"
    assert entry1["issue_created"] is True
    assert entry1["issue_url"] == "https://github.com/example/repo/issues/10"

    log_path = Path(entry1["log_path"])
    assert log_path.is_file()
    expected_log_dir = (home / ".local" / "state" / "gardenia" / "logs").resolve()
    assert log_path.resolve().parent == expected_log_dir
    assert "FAIL: regression in dep X" in log_path.read_text(encoding="utf-8")
    assert "FAIL: regression in dep X" in entry1["log_excerpt"]
    assert list(wt_root.iterdir()) == []

    # Second run: reuses existing open issue instead of filing a duplicate.
    second = run_cli(
        [
            "repos",
            "upgrade",
            "--repo",
            str(failing_repo),
            "--package",
            "pytest",
            "--file-issues",
            "--worktree-root",
            str(wt_root),
            "--json",
        ],
        cwd=tmp_path,
        env=env,
    )
    assert second.returncode == 1
    entry2 = json.loads(second.stdout)["repos"][0]
    assert entry2["status"] == "gate_failed"
    assert entry2["issue_created"] is False
    assert entry2["issue_url"] == entry1["issue_url"]

    gh_calls = _read_jsonl(gh_log)
    create_calls = [c for c in gh_calls if c["argv"][1:3] == ["issue", "create"]]
    assert len(create_calls) == 1


def test_repos_upgrade_agcoord_required_missing_and_human_outputs(tmp_path: Path) -> None:
    env, _, bin_dir = make_isolated_env(tmp_path)
    _ensure_bash_in_bin(bin_dir)

    repo = init_git_repo(tmp_path / "req-agc", env)
    write_executable(repo / "scripts" / "check", "#!/usr/bin/env bash\nexit 0\n")
    run_cli(["repo", "init", "--repo", str(repo), "--agcoord", "required", "--save-config"], cwd=tmp_path, env=env)
    _git(repo, env, "add", "-A")
    _git(repo, env, "commit", "-m", "Require agc")

    wt_root = tmp_path / "wt-root"
    fail = run_cli(
        ["repos", "upgrade", "--repo", str(repo), "--worktree-root", str(wt_root), "--json"],
        cwd=tmp_path,
        env=env,
    )
    assert fail.returncode == 1
    data = json.loads(fail.stdout)
    assert data["repos"][0]["status"] == "error"
    assert "agc" in data["repos"][0]["error"]
    assert list(wt_root.iterdir()) == []

    # Smoke-test human-readable (non-JSON) outputs for repos subcommands.
    assert run_cli(["repos", "list"], cwd=tmp_path, env=env).returncode == 0
    assert run_cli(["repos", "check"], cwd=tmp_path, env=env).returncode == 0
    assert run_cli(["repos", "prune", "--dry-run"], cwd=tmp_path, env=env).returncode == 0
    assert run_cli(["repos", "upgrade", "--dry-run"], cwd=tmp_path, env=env).returncode == 1


def test_repos_upgrade_uv_seed_multi_extras_and_dependency_groups(tmp_path: Path) -> None:
    env, _, bin_dir = make_isolated_env(tmp_path)
    _ensure_bash_in_bin(bin_dir)

    uv_log = tmp_path / "uv.jsonl"
    make_python_shim(
        bin_dir,
        "uv",
        """
        import pathlib, stat, sys
        if sys.argv[1:2] == ["venv"]:
            if "--seed" not in sys.argv:
                print("expected --seed flag on uv venv", file=sys.stderr)
                sys.exit(2)
            venv_dir = pathlib.Path(sys.argv[-1])
            vbin = venv_dir / "bin"
            vbin.mkdir(parents=True, exist_ok=True)
            py = vbin / "python"
            py.symlink_to(sys.executable)
            pip_marker = vbin / "pip"
            pip_marker.write_text("#!/usr/bin/env bash\\necho pip-seeded\\n", encoding="utf-8")
            pip_marker.chmod(pip_marker.stat().st_mode | stat.S_IXUSR)
            sys.exit(0)
        if sys.argv[1:3] == ["pip", "install"]:
            sys.exit(0)
        sys.exit(0)
        """,
        log_path=uv_log,
    )

    multi_repo = init_git_repo(tmp_path / "multi-extra-repo", env)
    write_executable(
        multi_repo / "scripts" / "check",
        "#!/usr/bin/env bash\nset -euo pipefail\ntest \"$(pip)\" = \"pip-seeded\"\n",
    )
    (multi_repo / "pyproject.toml").write_text(
        '[project]\n'
        'name = "multi-extra"\n'
        'version = "0.1.0"\n\n'
        '[project.optional-dependencies]\n'
        'test = ["pytest>=8"]\n'
        'docs = ["sphinx>=8"]\n'
        'dev = ["ruff>=0.9"]\n'
        'extra = ["ignored-extra"]\n\n'
        '[dependency-groups]\n'
        'lint = ["mypy>=1"]\n'
        'dev = [{include-group = "lint"}, "coverage>=7"]\n',
        encoding="utf-8",
    )
    (multi_repo / "docs").mkdir(parents=True, exist_ok=True)
    (multi_repo / "docs" / "requirements.txt").write_text("alabaster>=1\n", encoding="utf-8")
    run_cli(["repo", "init", "--repo", str(multi_repo), "--agcoord", "none", "--save-config", "--no-register"], cwd=tmp_path, env=env)
    _git(multi_repo, env, "add", "-A")
    _git(multi_repo, env, "commit", "-m", "Init multi-extra repo")

    dev_docs_repo = init_git_repo(tmp_path / "dev-and-docs-req-repo", env)
    write_executable(dev_docs_repo / "scripts" / "check", "#!/usr/bin/env bash\nset -euo pipefail\nexit 0\n")
    (dev_docs_repo / "pyproject.toml").write_text(
        '[project]\n'
        'name = "dev-only"\n'
        'version = "0.1.0"\n\n'
        '[project.optional-dependencies]\n'
        'dev = ["pytest>=8"]\n',
        encoding="utf-8",
    )
    (dev_docs_repo / "docs").mkdir(parents=True, exist_ok=True)
    (dev_docs_repo / "docs" / "requirements.txt").write_text("sphinx>=8\n", encoding="utf-8")
    run_cli(["repo", "init", "--repo", str(dev_docs_repo), "--agcoord", "none", "--save-config", "--no-register"], cwd=tmp_path, env=env)
    _git(dev_docs_repo, env, "add", "-A")
    _git(dev_docs_repo, env, "commit", "-m", "Init dev-only repo")

    wt_root = tmp_path / "wt-root"
    proc = run_cli(
        [
            "repos",
            "upgrade",
            "--repo",
            str(multi_repo),
            "--repo",
            str(dev_docs_repo),
            "--worktree-root",
            str(wt_root),
            "--json",
        ],
        cwd=tmp_path,
        env=env,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    data = json.loads(proc.stdout)
    by_name = {r["name"]: r for r in data["repos"]}
    assert by_name["multi-extra-repo"]["status"] == "up_to_date"
    assert by_name["dev-and-docs-req-repo"]["status"] == "up_to_date"

    uv_calls = _read_jsonl(uv_log)
    venv_calls = [c["argv"] for c in uv_calls if c["argv"][1:2] == ["venv"]]
    assert len(venv_calls) == 2
    assert all(call[1:3] == ["venv", "--seed"] for call in venv_calls)

    pip_calls = [c["argv"] for c in uv_calls if c["argv"][1:3] == ["pip", "install"]]
    multi_pip = [c for c in pip_calls if ".[test,docs,dev]" in c]
    assert len(multi_pip) == 1
    assert "mypy>=1" in multi_pip[0]
    assert "coverage>=7" in multi_pip[0]
    assert "ignored-extra" not in multi_pip[0]
    assert "docs/requirements.txt" not in multi_pip[0]

    dev_docs_pip = [c for c in pip_calls if ".[dev]" in c]
    assert len(dev_docs_pip) == 1
    assert "-r" in dev_docs_pip[0]
    assert "docs/requirements.txt" in dev_docs_pip[0]
    assert list(wt_root.iterdir()) == []


def test_repos_upgrade_pep735_dependency_groups_without_project(tmp_path: Path) -> None:
    env, _, bin_dir = make_isolated_env(tmp_path)
    _ensure_bash_in_bin(bin_dir)

    uv_log = tmp_path / "uv.jsonl"
    make_python_shim(
        bin_dir,
        "uv",
        """
        import pathlib, sys
        if sys.argv[1:3] == ["venv", "--seed"]:
            venv_dir = pathlib.Path(sys.argv[3])
            vbin = venv_dir / "bin"
            vbin.mkdir(parents=True, exist_ok=True)
            (vbin / "python").symlink_to(sys.executable)
            sys.exit(0)
        if sys.argv[1:3] == ["pip", "install"]:
            sys.exit(0)
        sys.exit(0)
        """,
        log_path=uv_log,
    )

    agrein_like = init_git_repo(tmp_path / "agrein-like", env)
    write_executable(
        agrein_like / "scripts" / "check",
        "#!/usr/bin/env bash\nset -euo pipefail\ntest -n \"${VIRTUAL_ENV:-}\"\n",
    )
    (agrein_like / "pyproject.toml").write_text(
        '[dependency-groups]\n'
        'typing = ["mypy>=1.10"]\n'
        'test = ["pytest>=8", {include-group = "typing"}]\n'
        'dev = [{include-group = "test"}, "PyYAML>=6"]\n',
        encoding="utf-8",
    )
    (agrein_like / "docs").mkdir(parents=True, exist_ok=True)
    (agrein_like / "docs" / "requirements.txt").write_text("sphinx>=8\nfuro\n", encoding="utf-8")
    run_cli(["repo", "init", "--repo", str(agrein_like), "--agcoord", "none", "--save-config", "--no-register"], cwd=tmp_path, env=env)
    _git(agrein_like, env, "add", "-A")
    _git(agrein_like, env, "commit", "-m", "Init PEP 735 tool repo")

    empty_no_project = init_git_repo(tmp_path / "empty-no-project", env)
    write_executable(empty_no_project / "scripts" / "check", "#!/usr/bin/env bash\nset -euo pipefail\nexit 0\n")
    (empty_no_project / "pyproject.toml").write_text('[tool.pytest.ini_options]\ntestpaths = ["tests"]\n', encoding="utf-8")
    run_cli(["repo", "init", "--repo", str(empty_no_project), "--agcoord", "none", "--save-config", "--no-register"], cwd=tmp_path, env=env)
    _git(empty_no_project, env, "add", "-A")
    _git(empty_no_project, env, "commit", "-m", "Init empty tool pyproject")

    wt_root = tmp_path / "wt-root"
    proc = run_cli(
        [
            "repos",
            "upgrade",
            "--repo",
            str(agrein_like),
            "--repo",
            str(empty_no_project),
            "--worktree-root",
            str(wt_root),
            "--json",
        ],
        cwd=tmp_path,
        env=env,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    by_name = {r["name"]: r for r in json.loads(proc.stdout)["repos"]}
    assert by_name["agrein-like"]["status"] == "up_to_date"
    assert by_name["empty-no-project"]["status"] == "up_to_date"

    uv_calls = _read_jsonl(uv_log)
    pip_calls = [c["argv"] for c in uv_calls if c["argv"][1:3] == ["pip", "install"]]
    assert len(pip_calls) == 1
    install_argv = pip_calls[0]
    assert "-e" not in install_argv
    assert "." not in install_argv
    assert "--upgrade" in install_argv
    assert "pytest>=8" in install_argv
    assert "mypy>=1.10" in install_argv
    assert "PyYAML>=6" in install_argv
    assert install_argv.count("mypy>=1.10") == 1
    assert "-r" in install_argv
    assert "docs/requirements.txt" in install_argv

    # Cyclic include-group references fail cleanly per-repo without crashing the CLI.
    cyclic = init_git_repo(tmp_path / "cyclic-groups", env)
    write_executable(cyclic / "scripts" / "check", "#!/usr/bin/env bash\nexit 0\n")
    (cyclic / "pyproject.toml").write_text(
        '[dependency-groups]\n'
        'a = [{include-group = "b"}]\n'
        'b = [{include-group = "a"}]\n'
        'dev = [{include-group = "a"}]\n',
        encoding="utf-8",
    )
    run_cli(["repo", "init", "--repo", str(cyclic), "--agcoord", "none", "--save-config", "--no-register"], cwd=tmp_path, env=env)
    _git(cyclic, env, "add", "-A")
    _git(cyclic, env, "commit", "-m", "Init cyclic dependency groups")

    cyc_proc = run_cli(
        ["repos", "upgrade", "--repo", str(cyclic), "--worktree-root", str(wt_root), "--json"],
        cwd=tmp_path,
        env=env,
    )
    assert cyc_proc.returncode == 1
    cyc_entry = json.loads(cyc_proc.stdout)["repos"][0]
    assert cyc_entry["status"] == "error"
    assert "Cyclic include-group" in cyc_entry["error"]
    assert list(wt_root.iterdir()) == []


def test_repos_upgrade_pre_gate_changed_files_ignores_gate_debris_and_stages_only_changed(tmp_path: Path) -> None:
    env, _, bin_dir = make_isolated_env(tmp_path)
    _ensure_bash_in_bin(bin_dir)

    agc_log = tmp_path / "agc.jsonl"
    gh_log = tmp_path / "gh.jsonl"
    gh_state = tmp_path / "gh-state.json"
    _install_agc_shim(bin_dir, agc_log)

    # Custom gh shim that records whether untracked gate debris was already removed before PR creation.
    debris_report = tmp_path / "gh-debris-check.json"
    make_python_shim(
        bin_dir,
        "gh",
        f"""
        import json, pathlib, sys
        state_file = pathlib.Path({str(gh_state)!r})
        report_file = pathlib.Path({str(debris_report)!r})
        state = json.loads(state_file.read_text(encoding="utf-8")) if state_file.exists() else {{"next_issue": 10, "next_pr": 100, "issues": [], "prs": [], "merged": []}}

        def save():
            state_file.write_text(json.dumps(state), encoding="utf-8")

        def get_opt(args, flag, default=""):
            if flag in args:
                idx = args.index(flag)
                if idx + 1 < len(args):
                    return args[idx + 1]
            return default

        argv = sys.argv[1:]
        if argv[:2] == ["issue", "create"]:
            num = state["next_issue"]
            state["next_issue"] += 1
            save()
            print(f"https://github.com/example/repo/issues/{{num}}")
            sys.exit(0)
        if argv[:2] == ["pr", "create"]:
            num = state["next_pr"]
            state["next_pr"] += 1
            save()
            report_file.write_text(json.dumps({{
                "gate_artifacts_exists": pathlib.Path("gate_artifacts").exists(),
                "stray_tmp_exists": pathlib.Path("stray.tmp").exists(),
            }}), encoding="utf-8")
            print(f"https://github.com/example/repo/pull/{{num}}")
            sys.exit(0)
        if argv[:2] == ["pr", "merge"]:
            save()
            print("Merged")
            sys.exit(0)
        sys.exit(2)
        """,
        log_path=gh_log,
    )

    debris_gate = (
        "#!/usr/bin/env bash\n"
        "set -euo pipefail\n"
        f"{sys.executable!r} -c \""
        "import pathlib; "
        "pathlib.Path('gate_artifacts/nested').mkdir(parents=True, exist_ok=True); "
        "pathlib.Path('gate_artifacts/nested/report.txt').write_text('artifact\\n', encoding='utf-8'); "
        "pathlib.Path('stray.tmp').write_text('stray\\n', encoding='utf-8'); "
        "open('README.md', 'a', encoding='utf-8').write('gate touched\\n')"
        "\"\n"
    )

    clean_repo, _ = _init_repo_with_bare_origin(
        tmp_path,
        "clean-debris",
        env,
        gate_body=debris_gate,
        agcoord="none",
        stale_agents=False,
    )
    drifted_repo, bare_drifted = _init_repo_with_bare_origin(
        tmp_path,
        "drifted-debris",
        env,
        gate_body=debris_gate,
        agcoord="auto",
        docs_exemption=True,
        stale_agents=True,
    )

    wt_root = tmp_path / "wt-root"

    # 1. Without --land: clean repo reports up_to_date (ignoring gate debris); drifted repo reports only AGENTS.md.
    verify_proc = run_cli(
        [
            "repos",
            "upgrade",
            "--repo",
            str(clean_repo),
            "--repo",
            str(drifted_repo),
            "--worktree-root",
            str(wt_root),
            "--json",
        ],
        cwd=tmp_path,
        env=env,
    )
    assert verify_proc.returncode == 0, verify_proc.stdout + verify_proc.stderr
    v_by_name = {r["name"]: r for r in json.loads(verify_proc.stdout)["repos"]}
    assert v_by_name["clean-debris"]["status"] == "up_to_date"
    assert v_by_name["clean-debris"]["changed_files"] == []
    assert v_by_name["drifted-debris"]["status"] == "changes_verified"
    assert v_by_name["drifted-debris"]["changed_files"] == ["AGENTS.md"]

    # 2. With --land: untracked gate debris is removed before commit, only AGENTS.md is committed/pushed.
    land_proc = run_cli(
        [
            "repos",
            "upgrade",
            "--repo",
            str(drifted_repo),
            "--land",
            "--worktree-root",
            str(wt_root),
            "--json",
        ],
        cwd=tmp_path,
        env=env,
    )
    assert land_proc.returncode == 0, land_proc.stdout + land_proc.stderr
    land_entry = json.loads(land_proc.stdout)["repos"][0]
    assert land_entry["status"] == "landed"
    assert land_entry["changed_files"] == ["AGENTS.md"]
    assert land_entry["land_method"] == "gh_pr_merge_docs_exemption"

    debris_status = json.loads(debris_report.read_text(encoding="utf-8"))
    assert debris_status == {"gate_artifacts_exists": False, "stray_tmp_exists": False}

    pushed_branch = land_entry["branch"]
    committed_files = [
        line.strip()
        for line in _git(bare_drifted, env, "show", "--name-only", "--pretty=format:", pushed_branch).splitlines()
        if line.strip()
    ]
    assert committed_files == ["AGENTS.md"]
    assert list(wt_root.iterdir()) == []


def test_repos_upgrade_gate_timeout_cli_forwarding_and_validation(tmp_path: Path) -> None:
    env, _, bin_dir = make_isolated_env(tmp_path)
    _ensure_bash_in_bin(bin_dir)

    repo = init_git_repo(tmp_path / "slow-gate", env)
    write_executable(
        repo / "scripts" / "check",
        f"#!/usr/bin/env bash\n{sys.executable!r} -c 'import time; time.sleep(2)'\n",
    )
    run_cli(["repo", "init", "--repo", str(repo), "--agcoord", "none", "--save-config", "--no-register"], cwd=tmp_path, env=env)
    _git(repo, env, "add", "-A")
    _git(repo, env, "commit", "-m", "Init slow gate repo")

    # Default --gate-timeout in --dry-run JSON is 7200.0.
    default_dry = run_cli(["repos", "upgrade", "--repo", str(repo), "--dry-run", "--json"], cwd=tmp_path, env=env)
    assert default_dry.returncode == 0
    assert json.loads(default_dry.stdout)["options"]["gate_timeout"] == 7200.0

    # Custom --gate-timeout forwards cleanly into options.
    custom_dry = run_cli(
        ["repos", "upgrade", "--repo", str(repo), "--gate-timeout", "42.5", "--dry-run", "--json"],
        cwd=tmp_path,
        env=env,
    )
    assert custom_dry.returncode == 0
    assert json.loads(custom_dry.stdout)["options"]["gate_timeout"] == 42.5

    # Invalid --gate-timeout values fail with exit code 2.
    for bad_timeout in ("0", "-5", "nan", "inf"):
        bad = run_cli(
            ["repos", "upgrade", "--repo", str(repo), "--gate-timeout", bad_timeout, "--dry-run", "--json"],
            cwd=tmp_path,
            env=env,
        )
        assert bad.returncode == 2, f"Expected exit 2 for --gate-timeout {bad_timeout!r}"

    # Execution honors --gate-timeout and times out quickly when gate exceeds it.
    wt_root = tmp_path / "wt-root"
    timed_out = run_cli(
        [
            "repos",
            "upgrade",
            "--repo",
            str(repo),
            "--gate-timeout",
            "0.2",
            "--worktree-root",
            str(wt_root),
            "--json",
        ],
        cwd=tmp_path,
        env=env,
    )
    assert timed_out.returncode == 1
    entry = json.loads(timed_out.stdout)["repos"][0]
    assert entry["status"] == "gate_failed"
    assert "timed out" in entry["log_excerpt"].lower()
    assert list(wt_root.iterdir()) == []


def test_repos_upgrade_agc_includes_tmpfs_inodes_when_tmpfs_gb_set(tmp_path: Path) -> None:
    env, _, bin_dir = make_isolated_env(tmp_path)
    _ensure_bash_in_bin(bin_dir)

    agc_log = tmp_path / "agc.jsonl"
    gh_log = tmp_path / "gh.jsonl"
    gh_state = tmp_path / "gh-state.json"
    _install_agc_shim(bin_dir, agc_log)
    _install_gh_shim(bin_dir, gh_state, gh_log)

    repo, _ = _init_repo_with_bare_origin(
        tmp_path,
        "tmpfs-repo",
        env,
        agcoord="required",
        docs_exemption=False,
        stale_agents=True,
    )
    run_cli(
        ["repo", "init", "--repo", str(repo), "--agcoord", "required", "--tmpfs-gb", "2", "--save-config", "--no-register"],
        cwd=tmp_path,
        env=env,
    )
    (repo / "AGENTS.md").write_text(f"# tmpfs-repo\n\n{MANAGED_BEGIN}\nstale\n{MANAGED_END}\n", encoding="utf-8")
    _git(repo, env, "add", "-A")
    _git(repo, env, "commit", "-m", "Configure tmpfs_gb=2 with stale AGENTS.md")
    _git(repo, env, "push", "origin", "main")

    wt_root = tmp_path / "wt-root"
    proc = run_cli(
        ["repos", "upgrade", "--repo", str(repo), "--land", "--worktree-root", str(wt_root), "--json"],
        cwd=tmp_path,
        env=env,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    entry = json.loads(proc.stdout)["repos"][0]
    assert entry["status"] == "landed"
    assert entry["land_method"] == "agc_land"

    agc_calls = _read_jsonl(agc_log)
    run_calls = [c["argv"] for c in agc_calls if len(c["argv"]) > 1 and c["argv"][1] == "run"]
    land_calls = [c["argv"] for c in agc_calls if len(c["argv"]) > 1 and c["argv"][1] == "land"]
    assert len(run_calls) == 1
    assert len(land_calls) == 1
    assert run_calls[0][1:4] == ["run", "--label", "upgrade gate (tmpfs-repo)"]
    pr_num = entry["pr_number"]
    assert land_calls[0][1:5] == ["land", str(pr_num), "--label", f"land #{pr_num} upgrade sweep (tmpfs-repo)"]
    for argv in (run_calls[0], land_calls[0]):
        assert "tmpfs=2147483648" in argv
        assert "tmpfs_inodes=262144" in argv
    assert list(wt_root.iterdir()) == []
