"""Hermetic behavioral tests for user-configured tools, plugins, sibling blocks, and stealth profiles."""

from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
import subprocess
import tomllib

from conftest import (
    init_git_repo,
    install_healthy_host_shims,
    make_isolated_env,
    make_python_shim,
    run_cli,
)
import pytest
from test_repos import (
    _ensure_bash_in_bin,
    _git,
    _init_repo_with_bare_origin,
    _install_venv_uv_shim,
    _read_jsonl,
    _run_upgrade,
)

from gardenia.host import (
    check_host,
    effective_host_config_path,
    load_host_config,
    resolve_tool_snippets,
    setup_host,
)
from gardenia.models import BUILTIN_PLUGINS, BUILTIN_TOOLS, MANAGED_BEGIN, MANAGED_END, GardeniaError
from gardenia.repo import (
    apply_sibling_block,
    check_repo,
    extract_sibling_block,
    init_repo,
    normalize_snippet,
    remove_sibling_block,
)
from gardenia.snippets import BUILTIN_SNIPPETS

BEGIN = "<!-- hotgate:begin -->"
END = "<!-- hotgate:end -->"
SNIPPET = "- Run `hotgate verify` before landing."

HOTGATE_CONFIG = """\
[[tools]]
name = "hotgate"
install_target = "git+ssh://git@github.com/example-owner/hotgate.git"
binary = "hotgate"
min_version = "0.1.0"
linux_only = false

[[plugins]]
name = "hotgate"
source = "git@github.com:example-owner/hotgate.git"
clients = ["codex", "claude", "agy"]
cli = "hotgate"
"""


@dataclass
class HotgateHost:
    env: dict[str, str]
    home: Path
    bin_dir: Path
    config_path: Path
    roots: dict[str, Path]
    tool_log: Path

    def write_snippet(self, text: str, *clients: str) -> None:
        for client in clients or tuple(self.roots):
            (self.roots[client] / "AGENTS.snippet.md").write_text(text, encoding="utf-8")


def _write_config(env: dict[str, str], text: str) -> Path:
    path = effective_host_config_path(env)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def _hotgate_host(
    tmp_path: Path,
    *,
    installed: tuple[str, ...] = ("codex", "claude", "agy"),
    versions: dict[str, str] | None = None,
    cli_version: str | None = "0.2.0",
    snippet: str | None = SNIPPET + "\n",
    config: str = HOTGATE_CONFIG,
) -> HotgateHost:
    """A healthy built-in stack plus a private ``hotgate`` tool and 3-host plugin."""

    env, home, bin_dir = make_isolated_env(tmp_path)
    versions = {client: "0.2.0" for client in installed} | (versions or {})
    roots = {
        client: (
            home / ".codex" / "plugins" / "cache" / "hotgate" / "hotgate" / versions[client]
            if client == "codex"
            else tmp_path / "plugin-roots" / client
        )
        for client in installed
    }
    builtin_agy = [
        {"name": spec.name, "enabled": True, "version": version}
        for spec, version in zip(BUILTIN_PLUGINS, ("0.1.0", "0.10.1", "6.0.0", "0.7.0"))
    ]
    builtin_claude = [
        {"id": f"{item['name']}@{item['name']}", "version": item["version"], "scope": "user", "enabled": True}
        for item in builtin_agy
    ]
    if "agy" in installed:
        builtin_agy.append(
            {"name": "hotgate", "enabled": True, "version": versions["agy"], "path": str(roots["agy"])}
        )
    if "claude" in installed:
        builtin_claude.append(
            {
                "id": "hotgate@hotgate",
                "version": versions["claude"],
                "scope": "user",
                "enabled": True,
                "installPath": str(roots["claude"]),
            }
        )
    install_healthy_host_shims(bin_dir, home, agy_plugins=builtin_agy, claude_plugins=builtin_claude)

    if "codex" in installed:
        with (home / ".codex" / "config.toml").open("a", encoding="utf-8") as fh:
            fh.write('\n[plugins."hotgate@hotgate"]\nenabled = true\n')
    for client, root in roots.items():
        root.mkdir(parents=True, exist_ok=True)
        (root / "plugin.json").write_text(
            json.dumps({"name": "hotgate", "version": versions[client]}), encoding="utf-8"
        )
        if snippet is not None:
            (root / "AGENTS.snippet.md").write_text(snippet, encoding="utf-8")

    tool_log = tmp_path / "hotgate.jsonl"
    if cli_version is not None:
        make_python_shim(bin_dir, "hotgate", f"print('hotgate {cli_version}')\n", log_path=tool_log)

    config_path = _write_config(env, config)
    return HotgateHost(env, home, bin_dir, config_path, roots, tool_log)


def _tool_repo(tmp_path: Path, env: dict[str, str], name: str = "repo") -> Path:
    repo = init_git_repo(tmp_path / name, env)
    (repo / "gardenia.toml").write_text('tools = ["hotgate"]\n', encoding="utf-8")
    return repo


def _cli_json(args: list[str], *, cwd: Path, env: dict[str, str], expect_exit: int = 0) -> dict:
    proc = run_cli([*args, "--json"], cwd=cwd, env=env)
    assert proc.returncode == expect_exit, proc.stdout + proc.stderr
    return json.loads(proc.stdout)


def _non_info(findings: list[dict]) -> list[dict]:
    return [item for item in findings if item["severity"] != "info"]


# ---------------------------------------------------------------------------
# Host configuration
# ---------------------------------------------------------------------------


def test_host_config_path_precedence_and_builtin_defaults(tmp_path: Path) -> None:
    env, home, _ = make_isolated_env(tmp_path)
    assert effective_host_config_path(env) == home / ".config" / "gardenia" / "config.toml"
    assert effective_host_config_path({"HOME": str(home)}) == home / ".config" / "gardenia" / "config.toml"
    assert effective_host_config_path({**env, "XDG_CONFIG_HOME": str(tmp_path / "xdg")}) == (
        tmp_path / "xdg" / "gardenia" / "config.toml"
    )
    override = {**env, "GARDENIA_HOST_CONFIG": "~/custom.toml"}
    assert effective_host_config_path(override) == home / "custom.toml"

    config = load_host_config(env)
    assert config.exists is False
    assert config.tools == BUILTIN_TOOLS
    assert config.plugins == BUILTIN_PLUGINS

    (home / "custom.toml").write_text(HOTGATE_CONFIG, encoding="utf-8")
    merged = load_host_config(override)
    assert [spec.name for spec in merged.tools] == ["howhard", "agroute", "agcoord", "hotgate"]
    assert [spec.name for spec in merged.plugins] == ["agrein", "agcoord", "howhard", "agroute", "hotgate"]


def test_host_config_source_forms_and_builtin_overrides(tmp_path: Path) -> None:
    env, home, _ = make_isolated_env(tmp_path)
    path = _write_config(
        env,
        """
[[tools]]
name = "from-index"
package = "index-dist[extra]>=1.0"

[[tools]]
name = "from-https"
install_target = "git+https://example.com/owner/from-https.git"

[[tools]]
name = "from-github"
install_target = "example-owner/from-github"

[[tools]]
name = "from-scp"
install_target = "git@example.com:example-owner/from-scp.git"

[[tools]]
name = "from-relative"
install_target = "./vendor/from-relative"

[[tools]]
name = "from-home"
install_target = "~/src/from-home"

[[tools]]
name = "howhard"
min_version = "6.1.0"

[[plugins]]
name = "local-plugin"
source = "../checkout"
clients = ["claude"]

[[plugins]]
name = "agrein"
source = "git+ssh://git@example.com/fork/agrein.git"
""",
    )
    config = load_host_config(env)
    targets = {spec.name: spec.target for spec in config.tools}
    assert targets == {
        "howhard": "howhard",
        "agroute": "agroute",
        "agcoord": "agcoord",
        "from-index": "index-dist[extra]>=1.0",
        "from-https": "git+https://example.com/owner/from-https.git",
        "from-github": "git+https://github.com/example-owner/from-github.git",
        "from-scp": "git+ssh://git@example.com/example-owner/from-scp.git",
        "from-relative": str(path.parent / "vendor" / "from-relative"),
        "from-home": str(home / "src" / "from-home"),
    }
    howhard = config.tool("howhard")
    assert howhard is not None and (howhard.min_version, howhard.binary) == ("6.1.0", "howhard")
    agrein = config.plugin("agrein")
    assert agrein is not None and agrein.clients == ("codex", "claude", "agy")
    local = config.plugin("local-plugin")
    assert local is not None and local.source == str(path.parent.parent / "checkout")

    actions = setup_host(clients=[], installer="uv", broker="skip", dry_run=True, env=_with_uv(env), platform="linux")
    commands = [action["command"] for action in actions["actions"] if action["command"]]
    assert ["uv", "tool", "install", "index-dist[extra]>=1.0"] in commands
    assert ["uv", "tool", "install", str(home / "src" / "from-home")] in commands


def _with_uv(env: dict[str, str]) -> dict[str, str]:
    bin_dir = Path(env["PATH"])
    make_python_shim(bin_dir, "uv", "pass\n")
    make_python_shim(bin_dir, "pipx", "pass\n")
    return env


@pytest.mark.parametrize(
    ("text", "fragment"),
    [
        ("[[tools]\n", "Invalid host gardenia configuration"),
        ("extra = 1\n", "unknown key(s): extra"),
        ('tools = "hotgate"\n', "array of tables"),
        ('[[tools]]\ninstall_target = "x"\n', "needs a 'name'"),
        ('[[tools]]\nname = "../evil"\ninstall_target = "x"\n', "needs a 'name'"),
        ('[[tools]]\nname = "gardenia"\ninstall_target = "x"\n', "needs a 'name'"),
        ('[[tools]]\nname = "a"\ninstall_target = "a"\nurl = "b"\n', "unknown key(s) in [[tools]]"),
        ('[[tools]]\nname = "a"\ninstall_target = "a"\n[[tools]]\nname = "a"\n', "duplicate [[tools]]"),
        ('[[tools]]\nname = "a"\n', "requires an 'install_target'"),
        ('[[tools]]\nname = "a"\ninstall_target = "a"\npackage = "a"\n', "only one of"),
        ('[[tools]]\nname = "a"\ninstall_target = "--index-url=x"\n', "not starting with '-'"),
        ('[[tools]]\nname = "a"\ninstall_target = "a b"\n', "must be a PyPI name"),
        ('[[tools]]\nname = "a"\ninstall_target = "git+http://example.com/a.git"\n', "must be a PyPI name"),
        ('[[tools]]\nname = "a"\ninstall_target = "a"\nmin_version = "1"\n', "MAJOR.MINOR.PATCH"),
        ('[[tools]]\nname = "a"\ninstall_target = "a"\nlinux_only = "no"\n', "must be a boolean"),
        ('[[tools]]\nname = "a"\ninstall_target = "a"\nbinary = "a b"\n', "must not contain whitespace"),
        ('[[plugins]]\nname = "p"\n', "requires a 'source'"),
        ('[[plugins]]\nname = "p"\nsource = "pypi-name"\n', "must be a Git URL"),
        ('[[plugins]]\nname = "p"\nsource = "o/p"\nclients = []\n', "non-empty list"),
        ('[[plugins]]\nname = "p"\nsource = "o/p"\nclients = ["vim"]\n', "non-empty list"),
        ('[[plugins]]\nname = "p"\nsource = "o/p"\ncli = "ghost"\n', "unknown tool cli"),
    ],
)
def test_host_config_validation_fails_with_exit_2(tmp_path: Path, text: str, fragment: str) -> None:
    env, _, _ = make_isolated_env(tmp_path)
    _write_config(env, text)
    with pytest.raises(GardeniaError) as excinfo:
        load_host_config(env)
    assert excinfo.value.exit_code == 2
    assert fragment in excinfo.value.message


def test_invalid_host_config_stops_host_commands_before_mutation(tmp_path: Path) -> None:
    env, _, bin_dir = make_isolated_env(tmp_path)
    uv_log = tmp_path / "uv.jsonl"
    make_python_shim(bin_dir, "uv", "pass\n", log_path=uv_log)
    _write_config(env, "extra = 1\n")
    for command in (["host", "check"], ["host", "setup"]):
        data = _cli_json(command, cwd=tmp_path, env=env, expect_exit=2)
        assert "unknown key(s): extra" in data["error"]
    assert not uv_log.exists()


def test_invalid_host_config_stops_repo_init_before_writing_builtin_blocks(tmp_path: Path) -> None:
    env, _, _ = make_isolated_env(tmp_path)
    repo = init_git_repo(tmp_path / "repo", env)
    _write_config(env, "extra = 1\n")
    data = _cli_json(["repo", "init", "--repo", str(repo), "--no-register"], cwd=tmp_path, env=env, expect_exit=2)
    assert "unknown key(s): extra" in data["error"]
    assert not (repo / "AGENTS.md").exists() and not (repo / "CLAUDE.md").exists()


# ---------------------------------------------------------------------------
# host setup / host check
# ---------------------------------------------------------------------------


def test_host_setup_installs_private_tool_and_registers_three_host_plugin(tmp_path: Path) -> None:
    env, home, bin_dir = make_isolated_env(tmp_path)
    log_path = tmp_path / "setup.jsonl"
    make_python_shim(bin_dir, "uv", "pass\n", log_path=log_path)
    for client in ("codex", "claude", "agy"):
        body = (
            "import sys\n"
            "if sys.argv[1:] == ['plugin', 'list', '--json']:\n"
            "    print('[]')\n"
        )
        make_python_shim(bin_dir, client, body, log_path=log_path)
    _write_config(env, HOTGATE_CONFIG)

    data = _cli_json(["host", "setup", "--broker", "skip"], cwd=tmp_path, env=env)
    executed = [a["command"] for a in data["actions"] if a["status"] == "executed" and "hotgate" in a["component"]]
    source = "git@github.com:example-owner/hotgate.git"
    assert executed == [
        ["uv", "tool", "install", "git+ssh://git@github.com/example-owner/hotgate.git"],
        ["codex", "plugin", "marketplace", "add", source],
        ["codex", "plugin", "add", "hotgate@hotgate"],
        ["claude", "plugin", "marketplace", "add", source],
        ["claude", "plugin", "install", "hotgate@hotgate", "--scope", "user"],
        ["agy", "plugin", "install", source],
    ]
    logged = [[Path(item["argv"][0]).name, *item["argv"][1:]] for item in _read_jsonl(log_path)]
    for command in executed:
        assert command in logged


@pytest.mark.parametrize(
    ("source", "marketplace", "agy_target"),
    [
        ("git+https://example.com/o/p.git", "https://example.com/o/p.git", "https://example.com/o/p.git"),
        ("git+ssh://git@example.com/o/p.git", "ssh://git@example.com/o/p.git", "ssh://git@example.com/o/p.git"),
        ("example-owner/p", "example-owner/p", "https://github.com/example-owner/p"),
        ("/srv/checkouts/p", "/srv/checkouts/p", "/srv/checkouts/p"),
    ],
)
def test_host_setup_translates_plugin_sources_per_client(
    tmp_path: Path, source: str, marketplace: str, agy_target: str
) -> None:
    env, _, bin_dir = make_isolated_env(tmp_path)
    for client in ("claude", "agy"):
        make_python_shim(bin_dir, client, "print('[]')\n")
    _write_config(env, f'[[plugins]]\nname = "p"\nsource = "{source}"\nclients = ["claude", "agy"]\n')

    res = setup_host(installer="auto", broker="skip", dry_run=True, env=_with_uv(env), platform="linux")
    commands = [a["command"] for a in res["actions"] if a["command"] and a["component"].endswith(":p")]
    assert ["claude", "plugin", "marketplace", "add", marketplace] in commands
    assert ["agy", "plugin", "install", agy_target] in commands


@pytest.mark.parametrize(
    ("installer", "target", "expected"),
    [
        ("uv", "hotgate-dist>=0.1", ["uv", "tool", "install", "--upgrade", "hotgate-dist>=0.1"]),
        ("pipx", "hotgate-dist>=0.1", ["pipx", "upgrade", "hotgate-dist"]),
        ("pipx", "git+https://example.com/o/hotgate.git", ["pipx", "upgrade", "hotgate"]),
        ("uv", "/srv/hotgate", ["uv", "tool", "install", "--upgrade", "--reinstall", "/srv/hotgate"]),
    ],
)
def test_host_setup_upgrades_configured_tool(
    tmp_path: Path, installer: str, target: str, expected: list[str]
) -> None:
    host = _hotgate_host(tmp_path, config=f'[[tools]]\nname = "hotgate"\ninstall_target = "{target}"\n')
    res = setup_host(
        clients=[], installer=installer, broker="skip", upgrade=True, dry_run=True, env=_with_uv(host.env)
    )
    assert [a["command"] for a in res["actions"] if a["component"] == "hotgate"] == [expected]


def test_host_check_audits_configured_tool_and_plugin(tmp_path: Path) -> None:
    host = _hotgate_host(tmp_path, versions={"claude": "0.1.9"})
    data = _cli_json(["host", "check"], cwd=tmp_path, env=host.env)
    assert data["host"]["clis"]["hotgate"]["status"] == "ok"
    assert data["host"]["clis"]["hotgate"]["version"] == "0.2.0"
    assert data["host"]["config"]["user_tools"] == ["hotgate"]
    assert [(f["scope"], f["severity"], f["component"]) for f in _non_info(data["findings"])] == [
        ("host", "warning", "claude:hotgate")
    ]
    assert "plugin is 0.1.9 while host CLI is 0.2.0" in _non_info(data["findings"])[0]["message"]
    _cli_json(["host", "check", "--strict"], cwd=tmp_path, env=host.env, expect_exit=1)


def test_host_check_reports_outdated_tool_missing_plugin_and_respects_client_list(tmp_path: Path) -> None:
    config = HOTGATE_CONFIG.replace('clients = ["codex", "claude", "agy"]', 'clients = ["claude", "agy"]')
    host = _hotgate_host(tmp_path, installed=("claude",), cli_version="0.0.9", config=config, versions={"claude": "0.0.9"})
    result = check_host(env=host.env, platform="linux")
    errors = {(f["component"], f["scope"]) for f in result["findings"] if f["severity"] == "error"}
    assert errors == {("hotgate", "host"), ("agy:hotgate", "host")}
    assert result["host"]["clis"]["hotgate"]["status"] == "outdated"
    assert "hotgate" not in result["host"]["clients"]["codex"]["plugins"]


def test_linux_only_configured_tool_is_skipped_elsewhere(tmp_path: Path) -> None:
    config = HOTGATE_CONFIG.replace("linux_only = false", "linux_only = true")
    host = _hotgate_host(tmp_path, cli_version=None, config=config)
    result = check_host(env=host.env, platform="darwin", with_agcoord=True)
    assert result["host"]["clis"]["hotgate"]["status"] == "skipped"
    assert result["host"]["clis"]["agcoord"]["status"] == "ok"


# ---------------------------------------------------------------------------
# AGENTS.snippet.md resolution and sibling block helpers
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("client", ["codex", "claude", "agy"])
def test_snippet_resolves_from_each_client_install_root_without_running_the_tool(
    tmp_path: Path, client: str
) -> None:
    host = _hotgate_host(tmp_path, installed=(client,))
    resolution = resolve_tool_snippets(("hotgate",), env=host.env)["hotgate"]
    assert resolution.body == SNIPPET
    assert resolution.origin == client
    assert resolution.path == str(host.roots[client] / "AGENTS.snippet.md")
    assert not host.tool_log.exists()


def test_snippet_resolution_prefers_highest_installed_version_then_client_order(tmp_path: Path) -> None:
    host = _hotgate_host(tmp_path, versions={"codex": "0.2.0", "claude": "0.3.0", "agy": "0.3.0"})
    for client in host.roots:
        host.write_snippet(f"- from {client}\n", client)
    assert resolve_tool_snippets(("hotgate",), env=host.env)["hotgate"].body == "- from claude"


def test_snippet_resolution_ignores_stale_codex_cache_and_uses_local_source_fallback(tmp_path: Path) -> None:
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    (checkout / "AGENTS.snippet.md").write_text(f"{BEGIN}\n- local copy\n{END}\n", encoding="utf-8")
    config = HOTGATE_CONFIG.replace("git@github.com:example-owner/hotgate.git\"\nclients", f'{checkout}"\nclients')
    host = _hotgate_host(tmp_path, installed=(), config=config)
    # A cached copy for a plugin that is no longer configured in Codex is not "installed".
    stale = host.home / ".codex" / "plugins" / "cache" / "hotgate" / "hotgate" / "9.0.0"
    stale.mkdir(parents=True)
    (stale / "plugin.json").write_text('{"version": "9.0.0"}', encoding="utf-8")
    (stale / "AGENTS.snippet.md").write_text("- stale\n", encoding="utf-8")

    resolution = resolve_tool_snippets(("hotgate",), env=host.env)["hotgate"]
    assert (resolution.origin, resolution.body, resolution.installed) == ("local-source", "- local copy", False)

    (checkout / "AGENTS.snippet.md").unlink()
    missing = resolve_tool_snippets(("hotgate",), env=host.env)["hotgate"]
    assert missing.body is None
    assert "not installed" in (missing.error or "")


@pytest.mark.parametrize(
    "text",
    ["", f"{BEGIN}\n{END}\n", f"a\n{BEGIN}\nb\n", f"{MANAGED_BEGIN}\nx\n{MANAGED_END}\n", "<!-- other:end -->"],
)
def test_invalid_snippets_are_rejected(text: str) -> None:
    with pytest.raises(GardeniaError):
        normalize_snippet("hotgate", text)


def test_sibling_block_upsert_is_idempotent_and_preserves_surroundings() -> None:
    original = f"# Title\n\n{MANAGED_BEGIN}\n- managed\n{MANAGED_END}\n\nProject notes.\n"
    first = apply_sibling_block(original, "hotgate", "- one")
    assert first == f"{original}\n{BEGIN}\n- one\n{END}\n"
    assert apply_sibling_block(first, "hotgate", "- one") == first

    trailing = first + "\nTrailing prose.\n"
    updated = apply_sibling_block(trailing, "hotgate", "- two")
    assert updated == trailing.replace("- one", "- two")
    assert extract_sibling_block(updated, "hotgate") == (f"{BEGIN}\n- two\n{END}", False)


def test_new_sibling_block_is_inserted_after_its_anchor_block() -> None:
    managed = f"{MANAGED_BEGIN}\n- managed\n{MANAGED_END}"
    original = f"# Title\n\n{managed}\n\nProject notes.\n"
    first = apply_sibling_block(original, "agrein", "- rein", after="gardenia")
    assert first == f"# Title\n\n{managed}\n\n<!-- agrein:begin -->\n- rein\n<!-- agrein:end -->\n\nProject notes.\n"
    second = apply_sibling_block(first, "hotgate", "- one", after="agrein")
    assert second == first.replace("<!-- agrein:end -->\n", f"<!-- agrein:end -->\n\n{BEGIN}\n- one\n{END}\n")
    # A missing anchor falls back to appending at the end of the file.
    assert apply_sibling_block(original, "hotgate", "- one", after="agroute").endswith(f"\n{BEGIN}\n- one\n{END}\n")
    assert apply_sibling_block(managed, "hotgate", "- one", after="gardenia") == f"{managed}\n\n{BEGIN}\n- one\n{END}\n"


@pytest.mark.parametrize(
    ("before", "after"),
    [
        (f"A\n\n{BEGIN}\nx\n{END}\n", "A\n"),
        (f"A\n\n{BEGIN}\nx\n{END}\n\nB\n", "A\n\nB\n"),
        (f"A\n{BEGIN}\nx\n{END}\nB\n", "A\nB\n"),
        ("A\n", "A\n"),
    ],
)
def test_sibling_block_removal_keeps_surrounding_spacing(before: str, after: str) -> None:
    assert remove_sibling_block(before, "hotgate") == after


@pytest.mark.parametrize(
    "text",
    [
        f"{BEGIN}\nx\n",
        f"{END}\nx\n{BEGIN}\n",
        f"{BEGIN}\nx\n{END}\n{BEGIN}\ny\n{END}\n",
        f"{BEGIN}\n\n{END}\n",
        f"{MANAGED_BEGIN}\n{BEGIN}\nx\n{END}\n{MANAGED_END}\n",
        f"{BEGIN}\n{MANAGED_BEGIN}\nx\n{END}\n{MANAGED_END}\n",
        f"{BEGIN}\n<!-- other:begin -->\nx\n{END}\n",
    ],
)
def test_malformed_or_overlapping_sibling_blocks_are_detected(text: str) -> None:
    assert extract_sibling_block(text, "hotgate") == (None, True)
    with pytest.raises(GardeniaError):
        apply_sibling_block(text, "hotgate", "- new")
    with pytest.raises(GardeniaError):
        remove_sibling_block(text, "hotgate")


# ---------------------------------------------------------------------------
# repo init / repo check
# ---------------------------------------------------------------------------


def test_repo_init_stamps_refreshes_and_preserves_sibling_blocks(tmp_path: Path) -> None:
    host = _hotgate_host(tmp_path)
    repo = init_git_repo(tmp_path / "repo", host.env)
    (repo / "AGENTS.md").write_text("# Rules\n\nKeep me.\n", encoding="utf-8")

    data = _cli_json(
        ["repo", "init", "--repo", str(repo), "--tool", "hotgate", "--save-config", "--no-register"],
        cwd=tmp_path,
        env=host.env,
    )
    assert data["unresolved_tools"] == []
    assert tomllib.loads((repo / "gardenia.toml").read_text(encoding="utf-8"))["tools"] == ["hotgate"]
    text = (repo / "AGENTS.md").read_text(encoding="utf-8")
    assert text.startswith("# Rules\n\nKeep me.\n")
    assert text.index(MANAGED_END) < text.index(BEGIN)
    assert f"{BEGIN}\n{SNIPPET}\n{END}\n" in text

    again = init_repo(repo_path=repo, save_config=True, register=False, env=host.env)
    assert {a["status"] for a in again["actions"]} == {"skipped"}

    host.write_snippet("- Refreshed guidance.\n")
    preview = init_repo(repo_path=repo, register=False, dry_run=True, env=host.env)
    assert [a["component"] for a in preview["actions"] if a["status"] == "planned"] == ["agents-md:hotgate"]
    assert (repo / "AGENTS.md").read_text(encoding="utf-8") == text
    init_repo(repo_path=repo, register=False, env=host.env)
    assert (repo / "AGENTS.md").read_text(encoding="utf-8") == text.replace(SNIPPET, "- Refreshed guidance.")


def test_repo_init_without_snippet_warns_and_leaves_block_untouched(tmp_path: Path) -> None:
    host = _hotgate_host(tmp_path, installed=())
    repo = _tool_repo(tmp_path, host.env)
    hand_written = f"# Rules\n\n{BEGIN}\n- committed elsewhere\n{END}\n"
    (repo / "AGENTS.md").write_text(hand_written, encoding="utf-8")

    result = init_repo(repo_path=repo, register=False, env=host.env)
    assert result["unresolved_tools"] == ["hotgate"]
    assert any("hotgate" in warning for warning in result["warnings"])
    assert (repo / "AGENTS.md").read_text(encoding="utf-8").startswith(hand_written)


def test_repo_init_refuses_malformed_sibling_markers_before_writing(tmp_path: Path) -> None:
    host = _hotgate_host(tmp_path)
    repo = _tool_repo(tmp_path, host.env)
    (repo / "AGENTS.md").write_text(f"{BEGIN}\nunterminated\n", encoding="utf-8")
    data = _cli_json(["repo", "init", "--repo", str(repo), "--no-register"], cwd=tmp_path, env=host.env, expect_exit=2)
    assert "Malformed hotgate markers" in data["error"]
    assert (repo / "AGENTS.md").read_text(encoding="utf-8") == f"{BEGIN}\nunterminated\n"
    assert not (repo / "CLAUDE.md").exists()


@pytest.mark.parametrize("value", ['"hotgate"', '["gardenia"]', '["a", "a"]', '["../x"]', "[1]"])
def test_repo_profile_rejects_invalid_tools(tmp_path: Path, value: str) -> None:
    env, _, _ = make_isolated_env(tmp_path)
    repo = init_git_repo(tmp_path / "repo", env)
    (repo / "gardenia.toml").write_text(f"tools = {value}\n", encoding="utf-8")
    result = check_repo(repo, env=env)
    assert result["ok"] is False
    assert result["repo"]["tracked"]["config"]["valid"] is False


def test_repo_check_and_render_are_deterministic_without_host_state(tmp_path: Path) -> None:
    host = _hotgate_host(tmp_path)
    repo = _tool_repo(tmp_path, host.env)
    init_repo(repo_path=repo, register=False, env=host.env)
    with_host = check_repo(repo, strict=True, env=host.env)

    # A CI-like machine: no plugin, no client, and an unreadable host config.
    bare_env, bare_home, _ = make_isolated_env(tmp_path / "ci")
    broken = bare_home / "broken.toml"
    broken.write_text("not toml [", encoding="utf-8")
    bare_env["GARDENIA_HOST_CONFIG"] = str(broken)
    without_host = check_repo(repo, strict=True, env=bare_env)
    assert without_host["ok"] is True, without_host["findings"]
    assert without_host["findings"] == with_host["findings"]
    assert without_host["repo"]["tracked"]["sibling_blocks"] == {
        name: {"present": True, "malformed": False} for name in ("agrein", "agroute", "agcoord", "hotgate")
    }
    rendered = _cli_json(["repo", "render", "--repo", str(repo)], cwd=tmp_path, env=bare_env)
    assert rendered["profile"]["tools"] == ["hotgate"]

    # Content drift is invisible offline, but marker presence and integrity are enforced.
    agents = repo / "AGENTS.md"
    text = agents.read_text(encoding="utf-8")
    agents.write_text(text.replace(SNIPPET, "- locally edited"), encoding="utf-8")
    assert check_repo(repo, strict=True, env=bare_env)["ok"] is True
    for broken_text in (text.replace(END, ""), text.split(BEGIN)[0]):
        agents.write_text(broken_text, encoding="utf-8")
        result = check_repo(repo, env=bare_env)
        assert [(f["severity"], f["component"]) for f in _non_info(result["findings"])] == [
            ("error", "agents-md:hotgate")
        ]
    assert not host.tool_log.exists()


# ---------------------------------------------------------------------------
# Combined check / repos check
# ---------------------------------------------------------------------------


def _combined(host: HotgateHost, tmp_path: Path, repo: Path, expect_exit: int) -> tuple[list[dict], list[dict]]:
    unified = _cli_json(
        ["check", "--repo", str(repo), "--strict"], cwd=tmp_path, env=host.env, expect_exit=expect_exit
    )
    fleet = _cli_json(
        ["repos", "check", "--repo", str(repo), "--strict"], cwd=tmp_path, env=host.env, expect_exit=expect_exit
    )
    assert fleet["repos"][0]["ok"] is (expect_exit == 0)
    return _non_info(unified["findings"]), _non_info(fleet["repos"][0]["findings"])


def test_combined_checks_verify_installed_snippet_sync(tmp_path: Path) -> None:
    host = _hotgate_host(tmp_path)
    repo = _tool_repo(tmp_path, host.env)
    init_repo(repo_path=repo, register=False, env=host.env)
    assert _combined(host, tmp_path, repo, 0) == ([], [])

    host.write_snippet("- Newer guidance.\n")
    for findings in _combined(host, tmp_path, repo, 1):
        assert [(f["scope"], f["severity"], f["component"]) for f in findings] == [
            ("repo", "warning", "agents-md:hotgate")
        ]
    assert check_repo(repo, strict=True, env=host.env)["ok"] is True


def test_combined_checks_report_missing_installation_once(tmp_path: Path) -> None:
    host = _hotgate_host(tmp_path)
    repo = _tool_repo(tmp_path, host.env)
    init_repo(repo_path=repo, register=False, env=host.env)
    (host.bin_dir / "hotgate").unlink()
    (host.home / ".codex" / "config.toml").write_text("", encoding="utf-8")

    unified, fleet = _combined(host, tmp_path, repo, 1)
    for findings in (unified, fleet):
        components = [f["component"] for f in findings if "hotgate" in f["component"]]
        assert components == ["hotgate", "codex:hotgate"]
        assert all(f["scope"] == "host" for f in findings)


def test_combined_checks_report_unconfigured_tool_and_missing_snippet(tmp_path: Path) -> None:
    host = _hotgate_host(tmp_path)
    repo = _tool_repo(tmp_path, host.env)
    init_repo(repo_path=repo, register=False, env=host.env)
    for root in host.roots.values():
        (root / "AGENTS.snippet.md").unlink()
    for findings in _combined(host, tmp_path, repo, 1):
        assert [(f["scope"], f["component"]) for f in findings] == [("host", "hotgate")]
        assert "ships no AGENTS.snippet.md" in findings[0]["message"]

    host.config_path.unlink()
    for findings in _combined(host, tmp_path, repo, 1):
        assert [(f["scope"], f["component"]) for f in findings] == [("host", "hotgate")]
        assert "not configured on this host" in findings[0]["message"]


def test_repos_check_reports_invalid_host_config_only_for_repos_with_tools(tmp_path: Path) -> None:
    host = _hotgate_host(tmp_path)
    with_tools = _tool_repo(tmp_path, host.env, "with-tools")
    init_repo(repo_path=with_tools, register=False, env=host.env)
    plain = init_git_repo(tmp_path / "plain", host.env)
    init_repo(repo_path=plain, register=False, env=host.env)
    host.config_path.write_text("extra = 1\n", encoding="utf-8")

    data = _cli_json(
        ["repos", "check", "--repo", str(with_tools), "--repo", str(plain)],
        cwd=tmp_path,
        env=host.env,
        expect_exit=1,
    )
    by_name = {item["name"]: item for item in data["repos"]}
    assert by_name["plain"]["ok"] is True
    assert [f["component"] for f in _non_info(by_name["with-tools"]["findings"])] == ["host-config"]


def _builtin_snippet_host(tmp_path: Path) -> tuple[HotgateHost, Path]:
    """A healthy host whose Claude Code install of ``agrein`` ships its own snippet."""

    host = _hotgate_host(tmp_path, installed=())
    root = tmp_path / "plugin-roots" / "agrein"
    root.mkdir(parents=True)
    (root / "AGENTS.snippet.md").write_text("<!-- agrein:begin -->\n- Installed agrein guidance.\n<!-- agrein:end -->\n")
    claude = [
        {"id": f"{spec.name}@{spec.name}", "version": version, "scope": "user", "enabled": True}
        for spec, version in zip(BUILTIN_PLUGINS, ("0.1.0", "0.10.1", "6.0.0", "0.7.0"))
    ]
    claude[0]["installPath"] = str(root)
    install_healthy_host_shims(host.bin_dir, host.home, claude_plugins=claude)
    host.config_path.unlink()
    return host, root / "AGENTS.snippet.md"


def test_builtin_snippets_resolve_installed_copy_then_builtin_fallback(tmp_path: Path) -> None:
    host, snippet = _builtin_snippet_host(tmp_path)
    resolutions = resolve_tool_snippets(("agrein", "agroute", "howhard", "agcoord"), env=host.env)
    assert (resolutions["agrein"].origin, resolutions["agrein"].body) == ("claude", "- Installed agrein guidance.")
    assert resolutions["agrein"].path == str(snippet)
    for name in ("agroute", "howhard", "agcoord"):
        # Installed, but this plugin version ships no snippet: the built-in copy applies.
        assert resolutions[name].installed is True
        assert (resolutions[name].origin, resolutions[name].path) == ("builtin", None)
        assert resolutions[name].body == BUILTIN_SNIPPETS[name].strip()

    bare_env, _, _ = make_isolated_env(tmp_path / "offline")
    offline = resolve_tool_snippets(("agrein",), env=bare_env)["agrein"]
    assert (offline.origin, offline.installed, offline.body) == ("builtin", False, BUILTIN_SNIPPETS["agrein"].strip())


def test_combined_checks_detect_builtin_block_drift_while_repo_check_stays_offline(tmp_path: Path) -> None:
    host, snippet = _builtin_snippet_host(tmp_path)
    repo = init_git_repo(tmp_path / "repo", host.env)
    result = init_repo(repo_path=repo, register=False, env=host.env)
    assert result["sibling_blocks"] == ["agrein", "agroute", "agcoord"]
    descriptions = {a["component"]: a["description"] for a in result["actions"]}
    assert descriptions["agents-md:agrein"] == f"Update AGENTS.md 'agrein' block from {snippet}."
    assert "built-in gardenia copy" in descriptions["agents-md:agcoord"]
    text = (repo / "AGENTS.md").read_text(encoding="utf-8")
    assert "<!-- agrein:begin -->\n- Installed agrein guidance.\n<!-- agrein:end -->" in text
    assert _combined(host, tmp_path, repo, 0) == ([], [])

    snippet.write_text("- Newer agrein guidance.\n", encoding="utf-8")
    for findings in _combined(host, tmp_path, repo, 1):
        assert [(f["scope"], f["severity"], f["component"]) for f in findings] == [
            ("repo", "warning", "agents-md:agrein")
        ]
        assert "installed AGENTS.snippet.md" in findings[0]["message"]

    # Without the installed copy, the block is compared with the built-in fallback instead.
    snippet.unlink()
    for findings in _combined(host, tmp_path, repo, 1):
        assert [f["component"] for f in findings] == ["agents-md:agrein"]
        assert "built-in gardenia copy" in findings[0]["message"]
    assert check_repo(repo, strict=True, env=host.env)["ok"] is True

    init_repo(repo_path=repo, register=False, env=host.env)
    assert _combined(host, tmp_path, repo, 0) == ([], [])


# ---------------------------------------------------------------------------
# Stealth profiles
# ---------------------------------------------------------------------------


def _status(repo: Path, env: dict[str, str]) -> str:
    return _git(repo, env, "status", "--porcelain", "--untracked-files=all")


def test_stealth_profile_never_touches_tracked_files(tmp_path: Path) -> None:
    host = _hotgate_host(tmp_path)
    repo = init_git_repo(tmp_path / "external", host.env)
    args = ["repo", "init", "--repo", str(repo), "--stealth", "--tool", "hotgate", "--gate", "make test"]

    preview = _cli_json([*args, "--dry-run"], cwd=tmp_path, env=host.env)
    profile_path = repo / ".git" / "gardenia" / "gardenia.toml"
    assert preview["stealth"] is True and not profile_path.exists()

    _cli_json(args, cwd=tmp_path, env=host.env)
    stored = tomllib.loads(profile_path.read_text(encoding="utf-8"))
    assert (stored["tools"], stored["gate"]) == (["hotgate"], "make test")
    assert _status(repo, host.env) == ""
    assert not (repo / "AGENTS.md").exists() and not (repo / "gardenia.toml").exists()

    # A later plain init (as run by fleet sweeps) stays stealthy and does not rewrite the profile.
    profile_path.write_text("# hand edited\n" + profile_path.read_text(encoding="utf-8"), encoding="utf-8")
    again = init_repo(repo_path=repo, register=False, env=host.env)
    assert again["stealth"] is True
    assert profile_path.read_text(encoding="utf-8").startswith("# hand edited\n")
    assert _status(repo, host.env) == ""

    result = check_repo(repo, strict=True, env=host.env)
    assert result["ok"] is True, result["findings"]
    assert result["repo"]["tracked"]["stealth"] is True
    assert result["repo"]["tracked"]["config"]["source"] == "stealth"
    assert _combined(host, tmp_path, repo, 0) == ([], [])

    listing = _cli_json(["repos", "list", "--strict"], cwd=tmp_path, env=host.env)
    assert [(r["managed"], r["agents_md_in_sync"]) for r in listing["repos"]] == [(True, None)]
    scanned = _cli_json(["repos", "scan", "--root", str(tmp_path), "--dry-run"], cwd=tmp_path, env=host.env)
    assert str(repo) in scanned["discovered"]

    human = run_cli(["repo", "check", "--repo", str(repo)], cwd=tmp_path, env=host.env)
    assert "stealth profile" in human.stdout
    assert "AGENTS.md hotgate block: not managed (stealth)" in human.stdout


def test_stealth_profile_is_shared_with_linked_worktrees(tmp_path: Path) -> None:
    host = _hotgate_host(tmp_path)
    repo = init_git_repo(tmp_path / "external", host.env)
    worktree = tmp_path / "external-wt"
    subprocess.run(
        ["git", "worktree", "add", "-b", "topic", str(worktree)],
        cwd=repo,
        env=host.env,
        check=True,
        capture_output=True,
    )
    init_repo(repo_path=worktree, stealth=True, tools=["hotgate"], env=host.env)
    assert check_repo(repo, env=host.env)["repo"]["tracked"]["config"]["values"]["tools"] == ["hotgate"]
    assert _status(worktree, host.env) == ""


def test_stealth_still_validates_host_readiness_and_flags_committed_markers(tmp_path: Path) -> None:
    host = _hotgate_host(tmp_path)
    repo = init_git_repo(tmp_path / "external", host.env)
    init_repo(repo_path=repo, stealth=True, tools=["hotgate"], register=False, env=host.env)
    (repo / "AGENTS.md").write_text(f"{BEGIN}\n- leaked\n{END}\n", encoding="utf-8")
    offline = check_repo(repo, env=host.env)
    assert [(f["severity"], f["component"]) for f in _non_info(offline["findings"])] == [("warning", "agents-md")]

    (repo / "AGENTS.md").unlink()
    (host.bin_dir / "hotgate").unlink()
    for findings in _combined(host, tmp_path, repo, 1):
        assert [(f["scope"], f["component"]) for f in findings] == [("host", "hotgate")]


def test_stealth_refuses_tracked_profiles_and_tracked_writes(tmp_path: Path) -> None:
    env, _, _ = make_isolated_env(tmp_path)
    tracked = init_git_repo(tmp_path / "tracked", env)
    (tracked / "gardenia.toml").write_text('gate = "make"\n', encoding="utf-8")
    with pytest.raises(GardeniaError, match="already has a tracked gardenia profile"):
        init_repo(repo_path=tracked, stealth=True, register=False, env=env)

    external = init_git_repo(tmp_path / "external", env)
    for kwargs in ({"save_config": True}, {"force_claude_symlink": True}):
        with pytest.raises(GardeniaError, match="cannot be used with a stealth profile"):
            init_repo(repo_path=external, stealth=True, register=False, env=env, **kwargs)
    assert not (external / ".git" / "gardenia").exists()

    # A tracked profile that appears later wins, and the leftover stealth file is reported.
    init_repo(repo_path=external, stealth=True, register=False, env=env)
    (external / "pyproject.toml").write_text('[tool.gardenia]\ngate = "tox"\n', encoding="utf-8")
    result = check_repo(external, env=env)
    assert result["repo"]["tracked"]["config"]["source"] == "pyproject.toml"
    assert any("stealth profile" in f["message"] for f in result["findings"] if f["severity"] == "warning")


def test_repos_upgrade_leaves_stealth_repository_files_alone(tmp_path: Path) -> None:
    host = _hotgate_host(tmp_path)
    _ensure_bash_in_bin(host.bin_dir)
    repo = init_git_repo(tmp_path / "external", host.env)
    init_repo(repo_path=repo, stealth=True, tools=["hotgate"], gate="true", register=False, env=host.env)

    plan = _run_upgrade(tmp_path, host.env, [repo], "--dry-run")["external"]
    assert not any(step.startswith("sync_sibling_block") for step in plan["planned_steps"])
    entry = _run_upgrade(tmp_path, host.env, [repo], "--package", "hotgate")["external"]
    assert (entry["status"], entry["changed_files"], entry["gate"]) == ("up_to_date", [], "true")
    assert _status(repo, host.env) == ""
    assert not (repo / "AGENTS.md").exists()


# ---------------------------------------------------------------------------
# repos upgrade
# ---------------------------------------------------------------------------


def _managed_tool_repo(
    tmp_path: Path, host: HotgateHost, name: str, extra_files: dict[str, str] | None = None
) -> Path:
    files = {"gardenia.toml": 'tools = ["hotgate"]\n', **(extra_files or {})}
    repo, _ = _init_repo_with_bare_origin(tmp_path, name, host.env, agcoord="none", extra_files=files)
    return repo


@pytest.mark.parametrize("package", ["hotgate", "hotgate==9.9.9"])
def test_repos_upgrade_package_refreshes_sibling_block_without_installing_the_tool(
    tmp_path: Path, package: str
) -> None:
    host = _hotgate_host(tmp_path)
    _ensure_bash_in_bin(host.bin_dir)
    uv_log = tmp_path / "uv.jsonl"
    cargo_log = tmp_path / "cargo.jsonl"
    _install_venv_uv_shim(host.bin_dir, uv_log)
    make_python_shim(host.bin_dir, "cargo", "import sys\nsys.exit(2)\n", log_path=cargo_log)
    locked = _managed_tool_repo(
        tmp_path, host, "locked", {"uv.lock": "version = 1\n", "Cargo.lock": "version = 3\n"}
    )
    venv = _managed_tool_repo(
        tmp_path, host, "venv-repo", {"pyproject.toml": '[project]\nname = "demo"\nversion = "0.1.0"\n'}
    )
    host.write_snippet("- Upgraded guidance.\n")

    plan = _run_upgrade(tmp_path, host.env, [locked], "--package", "hotgate", "--dry-run")["locked"]
    assert "sync_sibling_block:hotgate" in plan["planned_steps"]

    results = _run_upgrade(tmp_path, host.env, [locked, venv], "--package", package)
    for entry in results.values():
        # An exact tool name refreshes its block; a pinned spec is still never installed.
        assert entry["status"] == "changes_verified", entry
        assert entry["changed_files"] == ["AGENTS.md"]
        assert f"skip_dependency_upgrade:{package}:host-tool" in entry["upgrade_actions"]
    calls = [" ".join(item["argv"][1:]) for item in _read_jsonl(uv_log)]
    assert calls, "the pyproject repository still provisions its gate virtualenv"
    assert not any("hotgate" in call or call.startswith("lock") for call in calls)
    assert _read_jsonl(cargo_log) == []
    for repo in (locked, venv):
        assert _status(repo, host.env) == ""


def test_repos_upgrade_package_still_upgrades_real_dependency_sharing_a_tool_name(tmp_path: Path) -> None:
    host = _hotgate_host(tmp_path)
    _ensure_bash_in_bin(host.bin_dir)
    uv_log = tmp_path / "uv.jsonl"
    _install_venv_uv_shim(host.bin_dir, uv_log)
    repo = _managed_tool_repo(
        tmp_path,
        host,
        "consumer",
        {"pyproject.toml": '[project]\nname = "demo"\nversion = "0.1.0"\ndependencies = ["hotgate>=0.1"]\n'},
    )
    _run_upgrade(tmp_path, host.env, [repo], "--package", "hotgate")
    calls = [item["argv"][1:] for item in _read_jsonl(uv_log)]
    assert any(call[:2] == ["pip", "install"] and call[-2:] == ["--upgrade", "hotgate"] for call in calls)


def test_repos_upgrade_unresolved_snippet_is_skipped_unless_requested(tmp_path: Path) -> None:
    host = _hotgate_host(tmp_path)
    _ensure_bash_in_bin(host.bin_dir)
    repo = _managed_tool_repo(tmp_path, host, "consumer")
    committed = (repo / "AGENTS.md").read_text(encoding="utf-8")
    assert f"{BEGIN}\n{SNIPPET}\n{END}" in committed
    for root in host.roots.values():
        (root / "AGENTS.snippet.md").unlink()

    entry = _run_upgrade(tmp_path, host.env, [repo], "--no-upgrade-locks")["consumer"]
    assert entry["status"] == "up_to_date"
    assert "skip_sibling_block:hotgate:unresolved" in entry["upgrade_actions"]

    failed = _run_upgrade(tmp_path, host.env, [repo], "--package", "hotgate", expect_exit=1)["consumer"]
    assert failed["status"] == "error"
    assert "Could not resolve AGENTS.snippet.md for 'hotgate'" in failed["error"]
    assert (repo / "AGENTS.md").read_text(encoding="utf-8") == committed
    assert _status(repo, host.env) == ""
