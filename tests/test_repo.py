"""Hermetic behavioral tests for ``gardenia repo render|init|check``."""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess

from conftest import (
    init_git_repo,
    make_isolated_env,
    make_python_shim,
    run_cli,
)
from gardenia.models import MANAGED_BEGIN, MANAGED_END


def test_repo_render_outside_git_and_routing_modes(tmp_path: Path) -> None:
    env, _, _ = make_isolated_env(tmp_path)

    proc = run_cli(
        [
            "repo",
            "render",
            "--gate",
            "pytest -q",
            "--cpu",
            "2",
            "--memory-gb",
            "2",
            "--tmpfs-gb",
            "1",
            "--allow-overflow",
            "--routing",
            "agroute",
            "--agcoord",
            "required",
            "--no-docs-only-exemption",
        ],
        cwd=tmp_path,
        env=env,
    )
    assert proc.returncode == 0, proc.stderr
    out = proc.stdout
    assert out.startswith(MANAGED_BEGIN)
    assert out.strip().endswith(MANAGED_END)
    assert "agr candidates --host <codex|claude|agy>" in out
    assert (
        "  3. Select a route with `howhard` (`context --host <codex|claude|agy> --auto-decide`, or\n"
        "     `context`/`retrieve`/`decide` evaluating `baseline_request` in an independent stateless\n"
        "     context only when non-null, or `pick --host <codex|claude|agy>`), then resolve the\n"
        "     execution plan with\n"
        "     `agr plan --host <codex|claude|agy> --decision /tmp/TASK-decision.json --json`.\n"
        "  4. Honor `needs_review`."
    ) in out
    assert (
        '  - Check: `agc run --label "<short plain-English summary>" '
        '--resource cpu=2 --resource memory=2147483648 --resource tmpfs=1073741824 --resource tmpfs_inodes=262144 -- pytest -q`'
    ) in out
    assert (
        '  - Land: `agc land <pr> --label "land #<pr> <short summary>" '
        '--resource cpu=2 --resource memory=2147483648 --resource tmpfs=1073741824 --resource tmpfs_inodes=262144 -- pytest -q`'
    ) in out
    assert (
        '  Give every job a short plain-English `--label` for humans in `agc list` and `agc tui`\n'
        '  (lead with what is checked or landed; omit commit SHAs, branch names, `"pre-land"` tags,\n'
        '  and file paths).'
    ) in out
    assert "--no-overflow" not in out
    assert "documentation-only change" not in out

    proc_auto = run_cli(["repo", "render", "--agcoord", "auto"], cwd=tmp_path, env=env)
    assert proc_auto.returncode == 0, proc_auto.stderr
    auto_out = proc_auto.stdout
    assert (
        '  - Check: `agc run --label "<short plain-English summary>" '
        '--no-overflow --resource cpu=4 --resource memory=4294967296 -- bash scripts/check`'
    ) in auto_out
    assert (
        '  - Land: `agc land <pr> --label "land #<pr> <short summary>" '
        '--no-overflow --resource cpu=4 --resource memory=4294967296 -- bash scripts/check`'
    ) in auto_out
    assert (
        '  Give every job a short plain-English `--label` for humans in `agc list` and `agc tui`\n'
        '  (lead with what is checked or landed; omit commit SHAs, branch names, `"pre-land"` tags,\n'
        '  and file paths).'
    ) in auto_out

    proc_hh = run_cli(["repo", "render", "--routing", "howhard", "--agcoord", "none"], cwd=tmp_path, env=env)
    assert proc_hh.returncode == 0
    assert "agr candidates" not in proc_hh.stdout
    assert (
        "- Coordinate ticket work by delegating each authorized ticket to a native worker\n"
        "  selected with the installed `howhard` skill (`context --host <codex|claude|agy> --auto-decide`,\n"
        "  or `context`/`retrieve`/`decide` evaluating `baseline_request` in an independent stateless\n"
        "  context only when non-null, or `pick --host <codex|claude|agy>`), then record the actual\n"
        "  outcome and measured usage in `howhard` (`observe`, `usage attach`) and update `howhard`\n"
        "  memory; an assigned leaf worker implements its ticket directly without recursively\n"
        "  delegating it.\n"
        "- Track every code, test, documentation, or packaging change"
    ) in proc_hh.stdout
    assert "agc run" not in proc_hh.stdout


def test_repo_init_preserves_surrounding_prose_and_passes_strict_check(tmp_path: Path) -> None:
    env, _, _ = make_isolated_env(tmp_path)
    repo = init_git_repo(tmp_path / "myrepo", env)

    custom_agents = (
        "# My custom repo header\n\n"
        "Project-specific rule above.\n\n"
        f"{MANAGED_BEGIN}\n"
        "stale placeholder content\n"
        f"{MANAGED_END}\n\n"
        "## Custom footer\n\n"
        "Keep this footer intact byte-for-byte.\n"
    )
    (repo / "AGENTS.md").write_text(custom_agents, encoding="utf-8")

    init_proc = run_cli(
        ["repo", "init", "--repo", str(repo), "--gate", "bash scripts/check", "--cpu", "4", "--memory-gb", "4", "--save-config", "--json"],
        cwd=tmp_path,
        env=env,
    )
    assert init_proc.returncode == 0, init_proc.stderr
    updated = (repo / "AGENTS.md").read_text(encoding="utf-8")
    assert updated.startswith("# My custom repo header\n\nProject-specific rule above.\n\n<!-- gardenia:begin -->")
    assert updated.endswith("<!-- gardenia:end -->\n\n## Custom footer\n\nKeep this footer intact byte-for-byte.\n")
    assert "stale placeholder content" not in updated
    assert (repo / "CLAUDE.md").is_symlink()
    assert os.readlink(repo / "CLAUDE.md") == "AGENTS.md"
    assert (repo / "gardenia.toml").is_file()

    # Check passes strictly with zero findings.
    chk = run_cli(["repo", "check", "--repo", str(repo), "--strict", "--json"], cwd=tmp_path, env=env)
    assert chk.returncode == 0, chk.stdout + chk.stderr
    data = json.loads(chk.stdout)
    assert data["ok"] is True
    assert data["findings"] == []
    assert data["repo"]["tracked"]["agents_md"]["in_sync"] is True
    assert data["repo"]["tracked"]["claude_md"]["status"] == "symlink_to_agents"
    assert data["repo"]["tracked"]["config"]["source"] == "gardenia.toml"


def test_repo_init_refuses_linked_worktree_unless_allowed_and_non_git(tmp_path: Path) -> None:
    env, _, _ = make_isolated_env(tmp_path)
    # Non-git refusal
    non_git = tmp_path / "plain"
    non_git.mkdir()
    assert run_cli(["repo", "check", "--repo", str(non_git)], cwd=tmp_path, env=env).returncode == 2
    assert run_cli(["repo", "init", "--repo", str(non_git)], cwd=tmp_path, env=env).returncode == 2

    repo = init_git_repo(tmp_path / "primary", env)
    wt = tmp_path / "linked-wt"
    subprocess.run(["git", "worktree", "add", "-b", "feat-1", str(wt), "main"], cwd=repo, env=env, check=True, capture_output=True)

    refused = run_cli(["repo", "init", "--repo", str(wt), "--json"], cwd=tmp_path, env=env)
    assert refused.returncode == 2
    assert not (wt / "AGENTS.md").exists()

    allowed = run_cli(["repo", "init", "--repo", str(wt), "--allow-worktree", "--save-config", "--json"], cwd=tmp_path, env=env)
    assert allowed.returncode == 0, allowed.stderr
    assert (wt / "AGENTS.md").is_file()

    # repo check inside linked worktree flags is_linked_worktree and shares common .git dir.
    chk = run_cli(["repo", "check", "--repo", str(wt), "--json"], cwd=tmp_path, env=env)
    assert chk.returncode == 0
    data = json.loads(chk.stdout)
    assert data["repo"]["tracked"]["is_linked_worktree"] is True
    assert Path(data["repo"]["local_state"]["git_common_dir"]) == (repo / ".git").resolve()
    assert any(f["component"] == "worktree" and f["severity"] == "info" for f in data["findings"])


def test_repo_claude_md_regular_file_protection_and_force_override(tmp_path: Path) -> None:
    env, _, _ = make_isolated_env(tmp_path)
    repo = init_git_repo(tmp_path / "repo", env)
    (repo / "CLAUDE.md").write_text("custom claude instructions\n", encoding="utf-8")

    proc = run_cli(["repo", "init", "--repo", str(repo), "--json"], cwd=tmp_path, env=env)
    assert proc.returncode == 0
    payload = json.loads(proc.stdout)
    assert payload["warnings"]
    assert not (repo / "CLAUDE.md").is_symlink()
    assert (repo / "CLAUDE.md").read_text(encoding="utf-8") == "custom claude instructions\n"

    forced = run_cli(["repo", "init", "--repo", str(repo), "--force-claude-symlink", "--json"], cwd=tmp_path, env=env)
    assert forced.returncode == 0
    assert (repo / "CLAUDE.md").is_symlink()
    assert os.readlink(repo / "CLAUDE.md") == "AGENTS.md"


def test_repo_check_warns_non_destructively_on_legacy_howhard_json_and_pending_agr_prices(tmp_path: Path) -> None:
    env, _, bin_dir = make_isolated_env(tmp_path)
    repo = init_git_repo(tmp_path / "repo", env)
    run_cli(["repo", "init", "--repo", str(repo), "--save-config"], cwd=tmp_path, env=env)

    # Create pre-4.0 loose howhard JSON files inside .git/howhard.
    req_file = repo / ".git" / "howhard" / "requests" / "req-001.json"
    dec_file = repo / ".git" / "howhard" / "decisions" / "dec-001.json"
    req_file.parent.mkdir(parents=True, exist_ok=True)
    dec_file.parent.mkdir(parents=True, exist_ok=True)
    req_file.write_text('{"id": "req-001"}', encoding="utf-8")
    dec_file.write_text('{"id": "dec-001"}', encoding="utf-8")

    # Fake agr returning pending unpriced models.
    make_python_shim(
        bin_dir,
        "agr",
        """
        import json, sys
        if sys.argv[1:4] == ["prices", "pending", "--json"]:
            print(json.dumps({
                "format": "agroute-price-pending-v1",
                "pending_count": 2,
                "price_request": {
                    "models": [{"catalog_key": "openai/new-model-x"}, {"catalog_key": "google/new-model-y"}]
                },
            }))
            sys.exit(0)
        """,
    )

    chk = run_cli(["repo", "check", "--repo", str(repo), "--json"], cwd=tmp_path, env=env)
    assert chk.returncode == 0
    data = json.loads(chk.stdout)
    findings = data["findings"]
    assert all(f["scope"] == "repo" for f in findings)
    hh_warnings = [f for f in findings if f["component"] == "howhard-store"]
    assert len(hh_warnings) == 1
    assert "migrate-payloads.py" in hh_warnings[0]["remediation"]

    # Crucial: loose JSON files must NOT be deleted or modified!
    assert req_file.read_text(encoding="utf-8") == '{"id": "req-001"}'
    assert dec_file.read_text(encoding="utf-8") == '{"id": "dec-001"}'

    price_warnings = [f for f in findings if f["component"] == "agroute-prices"]
    assert len(price_warnings) == 1
    assert "openai/new-model-x" in price_warnings[0]["message"]
    assert data["repo"]["local_state"]["agroute_store"]["pending_price_count"] == 2

    # Strict mode fails on these warnings.
    strict = run_cli(["repo", "check", "--repo", str(repo), "--strict", "--json"], cwd=tmp_path, env=env)
    assert strict.returncode == 1


def test_repo_check_reads_pyproject_tool_gardenia_and_validates_optional_tomls(tmp_path: Path) -> None:
    env, _, _ = make_isolated_env(tmp_path)
    repo = init_git_repo(tmp_path / "repo", env)
    (repo / "pyproject.toml").write_text(
        '[tool.gardenia]\ngate = "pytest -q"\ncpu = 2\nmemory_gb = 2\nrouting = "howhard"\nagcoord = "none"\n',
        encoding="utf-8",
    )
    (repo / "howhard.toml").write_text("[invalid\n", encoding="utf-8")

    init_proc = run_cli(["repo", "init", "--repo", str(repo), "--json"], cwd=tmp_path, env=env)
    assert init_proc.returncode == 0
    assert "pytest -q" in (repo / "AGENTS.md").read_text(encoding="utf-8")

    chk = run_cli(["repo", "check", "--repo", str(repo), "--json"], cwd=tmp_path, env=env)
    assert chk.returncode == 1
    data = json.loads(chk.stdout)
    assert data["repo"]["tracked"]["config"]["source"] == "pyproject.toml"
    assert data["repo"]["tracked"]["howhard_toml"]["valid"] is False
    assert any(f["component"] == "howhard-toml" and f["severity"] == "error" for f in data["findings"])


def test_repo_init_refuses_malformed_markers(tmp_path: Path) -> None:
    env, _, _ = make_isolated_env(tmp_path)
    repo = init_git_repo(tmp_path / "repo", env)
    original = f"# Header\n\n{MANAGED_BEGIN}\nmissing end marker\n"
    (repo / "AGENTS.md").write_text(original, encoding="utf-8")

    proc = run_cli(["repo", "init", "--repo", str(repo), "--json"], cwd=tmp_path, env=env)
    assert proc.returncode == 2
    assert (repo / "AGENTS.md").read_text(encoding="utf-8") == original
