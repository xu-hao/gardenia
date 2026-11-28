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
from gardenia.snippets import BUILTIN_SNIPPETS


DEFAULT_BLOCK = """\
<!-- gardenia:begin -->
- File an issue (`#N — one sentence describing the work`) for each code, test, docs, or packaging
  change; work in an issue-named `/var/tmp` worktree from `origin/main`, keep the primary checkout
  read-only, and keep check logs complete (never truncated).
- Verify behavior at public boundaries with focused tests (bug fixes start with a failing one) and
  add canonical docs plus a `CHANGELOG.md` entry for user-facing changes. Tests stop and remove all
  they create and never touch `~/.codex`, `~/.claude`, `~/.gemini`, `~/.config`, or `~/.local`.
- Docs-only changes (every path `README.md`, `CHANGELOG.md`, `AGENTS.md`, Markdown in `docs/`, or
  a `docs/assets/` `.png`/`.svg` with provenance in `docs/assets/README.md`) skip tests and the
  gate; verify the PR file list and diff, then merge via the forge.
- Gate through `agc run` / `agc land <pr>` where `agc` is available, passing
  `--no-overflow --resource cpu=4 --resource memory=4294967296 -- bash scripts/check`; elsewhere,
  run `bash scripts/check` and merge via the forge after it passes.
- Never put private project names, machine-local paths, or other repositories' issue links in
  tracked files, commits, or pull requests.
- PyPI uploads and live host or production changes need explicit owner approval per operation.
<!-- gardenia:end -->
"""

# Tool usage minutiae belong in each plugin's installed skill, never in the gardenia block.
TOOL_TUTORIAL_FRAGMENTS = (
    "agr candidates",
    "agr plan",
    "price_request",
    "baseline_request",
    "--auto-decide",
    "matched_receipt",
    "needs_review",
    "ticket-delivery",
    "agc tui",
)


def _render(args: list[str], tmp_path: Path, env: dict[str, str]) -> dict:
    proc = run_cli(["repo", "render", *args, "--json"], cwd=tmp_path, env=env)
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


def test_repo_render_default_block_is_compact_and_free_of_tool_tutorials(tmp_path: Path) -> None:
    env, _, _ = make_isolated_env(tmp_path)

    proc = run_cli(["repo", "render"], cwd=tmp_path, env=env)
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout == DEFAULT_BLOCK
    assert len(DEFAULT_BLOCK.splitlines()) <= 18
    assert not any(fragment in DEFAULT_BLOCK for fragment in TOOL_TUTORIAL_FRAGMENTS)

    data = _render([], tmp_path, env)
    assert data["managed_block"] + "\n" == DEFAULT_BLOCK
    assert data["sibling_blocks"] == ["agrein", "agroute", "agcoord"]


def test_repo_render_follows_profile_parameters_and_routing_modes(tmp_path: Path) -> None:
    env, _, _ = make_isolated_env(tmp_path)

    required = _render(
        [
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
            "--tool",
            "hotgate",
        ],
        tmp_path,
        env,
    )
    block = required["managed_block"]
    assert block.startswith(MANAGED_BEGIN) and block.endswith(MANAGED_END)
    assert (
        "- Gate only through `agc run` / `agc land <pr>`, passing\n"
        "  `--resource cpu=2 --resource memory=2147483648 --resource tmpfs=1073741824 "
        "--resource tmpfs_inodes=262144 -- pytest -q`.\n"
    ) in block
    assert "--no-overflow" not in block
    assert "elsewhere" not in block
    assert "Docs-only" not in block
    assert required["sibling_blocks"] == ["agrein", "agroute", "agcoord", "hotgate"]

    howhard = _render(["--routing", "howhard", "--agcoord", "none"], tmp_path, env)
    assert "agc run" not in howhard["managed_block"] and "agc land" not in howhard["managed_block"]
    assert "- Run `bash scripts/check` before every merge, and merge via the forge only after it passes.\n" in (
        howhard["managed_block"]
    )
    assert howhard["sibling_blocks"] == ["agrein", "howhard"]

    bare = _render(["--routing", "none", "--agcoord", "none"], tmp_path, env)
    assert bare["sibling_blocks"] == ["agrein"]
    for data in (required, howhard, bare):
        assert not any(fragment in data["managed_block"] for fragment in TOOL_TUTORIAL_FRAGMENTS)
        for name in data["sibling_blocks"]:
            assert f"<!-- {name}:begin -->" not in data["managed_block"]


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
    # Built-in blocks are grouped right after the managed block, ahead of the untouched footer.
    assert updated.endswith("<!-- agcoord:end -->\n\n## Custom footer\n\nKeep this footer intact byte-for-byte.\n")
    markers = [MANAGED_END, "<!-- agrein:begin -->", "<!-- agroute:begin -->", "<!-- agcoord:begin -->", "## Custom"]
    assert [updated.index(marker) for marker in markers] == sorted(updated.index(marker) for marker in markers)
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


def _block(name: str) -> str:
    return f"<!-- {name}:begin -->\n{BUILTIN_SNIPPETS[name].strip()}\n<!-- {name}:end -->"


def _findings(result: dict) -> list[tuple[str, str]]:
    return sorted((f["severity"], f["component"]) for f in result["findings"] if f["severity"] != "info")


def test_repo_init_stamps_builtin_blocks_offline_and_follows_profile_changes(tmp_path: Path) -> None:
    env, _, _ = make_isolated_env(tmp_path)  # no agent client and no plugin cache on this host
    repo = init_git_repo(tmp_path / "repo", env)
    agents = repo / "AGENTS.md"
    agents.write_text("# Rules\n\nKeep me.\n", encoding="utf-8")

    first = run_cli(["repo", "init", "--repo", str(repo), "--save-config", "--no-register", "--json"], cwd=tmp_path, env=env)
    assert first.returncode == 0, first.stderr
    data = json.loads(first.stdout)
    assert data["sibling_blocks"] == ["agrein", "agroute", "agcoord"]
    assert data["unresolved_tools"] == [] and data["warnings"] == []
    text = agents.read_text(encoding="utf-8")
    assert text == (
        "# Rules\n\nKeep me.\n\n"
        f"{data['managed_block']}\n\n{_block('agrein')}\n\n{_block('agroute')}\n\n{_block('agcoord')}\n"
    )
    assert "howhard:begin" not in text
    chk = run_cli(["repo", "check", "--repo", str(repo), "--strict", "--json"], cwd=tmp_path, env=env)
    assert chk.returncode == 0, chk.stdout
    assert json.loads(chk.stdout)["repo"]["tracked"]["sibling_blocks"] == {
        name: {"present": True, "malformed": False} for name in ("agrein", "agroute", "agcoord")
    }

    again = run_cli(["repo", "init", "--repo", str(repo), "--no-register", "--json"], cwd=tmp_path, env=env)
    assert {a["status"] for a in json.loads(again.stdout)["actions"]} == {"skipped"}
    assert agents.read_text(encoding="utf-8") == text

    # Switching routing and agcoord is visible offline before re-stamping.
    (repo / "gardenia.toml").write_text('routing = "howhard"\nagcoord = "none"\n', encoding="utf-8")
    drifted = run_cli(["repo", "check", "--repo", str(repo), "--json"], cwd=tmp_path, env=env)
    assert drifted.returncode == 0, drifted.stdout
    assert _findings(json.loads(drifted.stdout)) == [
        ("warning", "agents-md"),
        ("warning", "agents-md:agcoord"),
        ("warning", "agents-md:agroute"),
        ("warning", "agents-md:howhard"),
    ]

    switched = run_cli(["repo", "init", "--repo", str(repo), "--no-register", "--json"], cwd=tmp_path, env=env)
    assert switched.returncode == 0, switched.stderr
    planned = {a["component"]: a["description"] for a in json.loads(switched.stdout)["actions"] if a["status"] == "executed"}
    assert set(planned) == {"agents-md", "agents-md:howhard", "agents-md:agroute", "agents-md:agcoord"}
    assert planned["agents-md:agroute"].startswith("Remove AGENTS.md 'agroute' block")
    block = json.loads(switched.stdout)["managed_block"]
    assert agents.read_text(encoding="utf-8") == (
        f"# Rules\n\nKeep me.\n\n{block}\n\n{_block('agrein')}\n\n{_block('howhard')}\n"
    )
    strict = run_cli(["repo", "check", "--repo", str(repo), "--strict", "--json"], cwd=tmp_path, env=env)
    assert strict.returncode == 0, strict.stdout

    # A deleted built-in block is a warning that repo init can always repair offline.
    agents.write_text(agents.read_text(encoding="utf-8").replace(f"\n\n{_block('howhard')}", ""), encoding="utf-8")
    missing = run_cli(["repo", "check", "--repo", str(repo), "--json"], cwd=tmp_path, env=env)
    assert missing.returncode == 0
    assert _findings(json.loads(missing.stdout)) == [("warning", "agents-md:howhard")]
    assert run_cli(["repo", "check", "--repo", str(repo), "--strict"], cwd=tmp_path, env=env).returncode == 1
    run_cli(["repo", "init", "--repo", str(repo), "--no-register"], cwd=tmp_path, env=env)
    assert agents.read_text(encoding="utf-8").endswith(f"{_block('agrein')}\n\n{_block('howhard')}\n")


def test_repo_init_refuses_malformed_builtin_markers_before_writing(tmp_path: Path) -> None:
    env, _, _ = make_isolated_env(tmp_path)
    repo = init_git_repo(tmp_path / "repo", env)
    # agroute is inactive under this profile, yet its broken markers still block the write.
    (repo / "gardenia.toml").write_text('routing = "none"\n', encoding="utf-8")
    original = "# Rules\n\n<!-- agroute:begin -->\nunterminated\n"
    (repo / "AGENTS.md").write_text(original, encoding="utf-8")

    proc = run_cli(["repo", "init", "--repo", str(repo), "--no-register", "--json"], cwd=tmp_path, env=env)
    assert proc.returncode == 2
    assert "Malformed agroute markers" in json.loads(proc.stdout)["error"]
    assert (repo / "AGENTS.md").read_text(encoding="utf-8") == original
