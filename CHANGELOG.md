# Changelog

All notable changes to `gardenia` are documented in this file.

## Unreleased

- **Breaking template change:** the `<!-- gardenia:begin --> ... <!-- gardenia:end -->`
  block is now compact. For the default profile it drops from 59 to 17 lines and keeps
  only repository policy and profile parameters: issue-first tracking, `/var/tmp`
  worktrees with a read-only primary checkout, complete check logs, behavioral testing,
  the documentation-only exemption, the gate command with integer byte `agc` claims,
  hermetic tests, no references to private projects or other repositories' issues, and
  per-operation approval for PyPI and live changes. Step-by-step `agr`, `howhard`, and
  `agc` CLI details now come only from each tool's installed skill. Run
  `gardenia repo init` or `gardenia repos upgrade` to refresh existing repositories
  (`#35`).
- `gardenia repo init` and `gardenia repos upgrade` now stamp a sibling block for each
  active built-in plugin: `agrein` always, `agroute` or `howhard` according to `routing`,
  and `agcoord` unless `agcoord = "none"`. Each block comes from the installed plugin's
  `AGENTS.snippet.md`, or from a verbatim copy shipped inside `gardenia` when no installed
  plugin provides one, so offline hosts stamp a deterministic block. Built-in and new tool
  blocks are inserted right after the `gardenia` block instead of at the end of the file,
  and `repo init` removes the block of a built-in plugin the profile no longer activates
  (`#35`).
- `gardenia repo check` still reads repository files only. It warns when an active
  built-in block is missing or a deactivated one remains, while `gardenia check` and
  `gardenia repos check` also flag built-in blocks that drift from the snippet this host
  would stamp. `gardenia repo render --json` and `gardenia repo init --json` list the
  expected `sibling_blocks` (`#35`).
- Added user-configured CLI tools and agent plugins, including private ones, through a host
  file at `${XDG_CONFIG_HOME:-~/.config}/gardenia/config.toml` (or
  `$GARDENIA_HOST_CONFIG`). `[[tools]]` and `[[plugins]]` entries merge over the built-in
  `agrein`, `agroute`, `howhard`, and `agcoord` stack, and `gardenia host setup` /
  `gardenia host check` manage and audit them across `codex`, `claude`, and `agy`
  (`#36`).
- Added a `tools` list to the repository profile (and a repeatable `--tool` flag).
  `gardenia repo init` and `gardenia repos upgrade` stamp one
  `<!-- <tool>:begin --> ... <!-- <tool>:end -->` block per tool from the installed
  plugin's `AGENTS.snippet.md`. `gardenia repo check` verifies marker presence and
  integrity from repository files alone, while `gardenia check` and
  `gardenia repos check` also verify host installation and block content (`#36`).
- Added `gardenia repo init --stealth`, an untracked profile under
  `<git-common-dir>/gardenia/` for external or shared repositories that must not receive
  committed `gardenia` files or markers (`#36`).
- `gardenia repos upgrade --package <name>` no longer passes a declared or
  user-configured tool name to `uv`, `pip`, or `cargo` unless the repository depends on
  that package, so a private tool name cannot resolve to an unrelated public package
  (`#36`).

Example: a private merge-gate toolkit named `hotgate`. In the host `config.toml`:

```toml
[[tools]]
name = "hotgate"
install_target = "git+ssh://git@github.com/<owner>/hotgate.git"
binary = "hotgate"
min_version = "0.1.0"
linux_only = false

[[plugins]]
name = "hotgate"
source = "git@github.com:<owner>/hotgate.git"
clients = ["codex", "claude", "agy"]
cli = "hotgate"
```

Then opt a repository in through `gardenia.toml` and run `gardenia repo init`:

```toml
gate = "bash scripts/check"
cpu = 2
memory_gb = 4
tools = ["hotgate"]
```

## 0.2.5 — 2026-11-28

- Fixed `gardenia repos upgrade --no-upgrade-locks` skipping test-virtualenv provisioning
  for `pyproject.toml` repositories without `uv.lock`, which ran the gate without the
  project or its test dependencies. The virtualenv is now provisioned whenever the gate
  runs; only dependency upgrades are disabled (`#33`).
- `gardenia repos upgrade --no-upgrade-locks` with policy sync now skips the gate and
  `agc run` when nothing changed (`up_to_date`), or when `docs_exemption` is enabled and
  every changed file qualifies (`#33`).
- `gardenia repos upgrade` now commits the changed files to a temporary local candidate
  branch before running the gate, so gates that require a clean checkout pass. Source-tree
  changes left by virtualenv setup are discarded before the gate, and changes left by a
  passing gate are discarded before `--land` publishes (`#33`).

## 0.2.4 — 2026-11-27

- Raised the minimum required host CLI versions in `gardenia host check`: `howhard` from
  `5.0.0` to `6.0.0`, and `agroute` (`agr`) from `0.6.0` to `0.7.0` (`#31`).
- Updated the rendered `AGENTS.md` routing block (`gardenia repo init` /
  `gardenia repo render` for both `routing = "agroute"` and `routing = "howhard"`) and
  canonical documentation for `howhard 6.0.0`, passing `--host <codex|claude|agy>` on
  `howhard context --auto-decide` and `howhard pick` (`#31`).

## 0.2.3 — 2026-11-24

- Raised the minimum required `howhard` host CLI version in `gardenia host check` from
  `4.0.0` to `5.0.0` (`#29`).
- Updated the rendered `AGENTS.md` routing block (`gardenia repo init` /
  `gardenia repo render` for both `routing = "agroute"` and `routing = "howhard"`) and
  canonical documentation for `howhard 5.0.0` (`context --auto-decide`, evaluating
  `baseline_request` in an independent stateless context only when non-null during
  `context`/`retrieve`/`decide`, or `pick`) (`#29`).

## 0.2.2 — 2026-11-23

- Pass explicit human-readable `--label` flags in rendered `AGENTS.md` `agc run` /
  `agc land` blocks (with queue-display guidance for `agc list` and `agc tui`) and in
  `gardenia repos upgrade` invocations (`#23`).
- Streamlined `README.md` and the Read the Docs overview (`docs/index.md`,
  `docs/architecture.md`, `docs/quickstart.md`) around the *digital artifact factory in a
  box* framing, separating human-facing setup, fleet commands, and live terminal UIs
  (`gardenia`, `agr tui`, `agc tui`) from agent-operated workflow, routing, sizing, and
  merge-queue tools (`agrein`, `agroute`, `howhard`, `agcoord`) (`#19`, `#21`, `#25`).

## 0.2.1 — 2026-11-22

- Hardened `gardenia repos upgrade` virtual environment provisioning, pre-gate diff
  isolation, and gate timeouts (`#15`):
  - Provision temporary virtual environments with `uv venv --seed` so `python -m pip`
    is available inside repository validation gates.
  - Updated `pyproject.toml` provisioning to install all present `("test", "docs", "dev")`
    entries in `[project.optional-dependencies]` (`-e ".[...]"`), resolve PEP 735
    `[dependency-groups]` (`dev`, `test`, `docs`, including recursive
    `include-group` entries), and install `-r docs/requirements.txt` when `"docs"` is
    not already in `[project.optional-dependencies]`; for tool-only repositories without
    `[project]` in `pyproject.toml`, skip `-e .` and install `[dependency-groups]` plus
    `docs/requirements.txt` with `--upgrade`.
  - Capture `changed_files` before running `profile.gate`, remove untracked gate
    artifacts before creating `--land` branches/commits, and stage only `changed_files`
    (`git add -- ...`) instead of `git add -A`.
  - Raised default `GATE_TIMEOUT_SECONDS` from `1800.0` to `7200.0` and added
    `--gate-timeout SECONDS`.
  - Appended `--resource tmpfs_inodes=262144` whenever `tmpfs_gb` is set in both
    rendered `AGENTS.md` `agc` commands (`gardenia repo init|render`) and
    `gardenia repos upgrade` (`agc run` / `agc land`).
- Replaced `docs/assets/gardenia.svg` with five fractal-sized outlined hexagons along an
  open arc and updated provenance in `docs/assets/README.md` (`#16`).

## 0.2.0 — 2026-11-22

- Added `gardenia repos` multi-repository host registry, fleet check, and
  isolated-worktree policy/dependency upgrade sweep (`#9`):
  - Host repository registry at `${GARDENIA_REPOS_CONFIG:-${XDG_CONFIG_HOME:-~/.config}/gardenia/repos.toml}`
    (`format = "gardenia-repos-v1"`) with canonical atomic writes and subcommands
    `gardenia repos list`, `add`, `remove`, `scan`, and `prune`.
  - Integrated automatic primary-checkout registration into `gardenia repo init`
    (`--register` / `--no-register`), skipping linked Git worktrees automatically (and
    refusing explicit `--register` on linked worktrees with exit code `2`).
  - Added `gardenia repos check` (`gardenia-repos-check-v1`) to verify all registered,
    discovered, or explicit `--repo` checkouts in one read-only pass.
  - Added `gardenia repos upgrade` (`gardenia-repos-upgrade-v1`) to sync policy, refresh
    `uv.lock` / `Cargo.lock` or provision fresh virtual environments for unpinned
    `pyproject.toml` repositories, run each repository's configured gate (via `agc run`
    when applicable) inside disposable isolated worktrees without ever mutating primary
    checkouts, persist failure logs under `XDG_STATE_HOME` / `--log-dir`, optionally
    file deduplicated GitHub issues (`--file-issues`), and optionally open and land
    verified updates (`--land`, honoring `docs_exemption`).
- Replaced `docs/assets/gardenia.svg` with the borderless four-blossom Fibonacci
  Golden-Rectangle *Gardenia jasminoides* mark (`89 : 55 : 34 : 21` for `agrein`,
  `agcoord`, `howhard`, and `agroute`) and updated provenance in
  `docs/assets/README.md` (`#7`, `#10`).

## 0.1.1 — 2026-11-22

- Bounded the AGCoord broker probe in `gardenia host check` with `agc --json list --limit 1`
  and raised `BROKER_PROBE_TIMEOUT_SECONDS` from `10.0s` to `30.0s` so large historical run
  counts and concurrent cgroup activity do not trigger false probe timeouts (`#3`).
- Distinguished broker probe timeouts (`reason: "timeout"`) from missing/stopped brokers:
  `gardenia host check` now emits a non-destructive `agc --json list --limit 1` remediation
  instead of suggesting `agc host install --user`, and `gardenia host setup` refuses to
  mutate the AGCoord broker (exit code `2`) when the probe times out unless `--broker skip`
  or `--force-broker` is passed (`#3`).

## 0.1.0 — 2026-11-22

- Initial `gardenia` `0.1.0` zero-dependency CLI (`Python 3.11+`, console script `gardenia`)
  implementing strict **per-host** vs. **per-repository** separation:
  - `gardenia host check` — read-only inspection of `howhard`, `agr`, Linux `agc` CLI
    versions and broker enforcement mode (`cgroup-v2` vs. `admission-only`), detected
    coding-agent clients (`codex`, `claude`, `agy`), user-scoped plugin versions and
    CLI-vs-plugin drift (inspecting Codex via `~/.codex/config.toml` and cached
    `plugin.json` without invoking interactive `codex plugin list`), and
    `~/.config/agroute/config.toml` reserve settings.
  - `gardenia host setup` — idempotent installation/upgrade of host CLIs (`uv tool` /
    `pipx`), enforced-broker-safe `agc host` setup (`--broker auto|user|enforced|skip`),
    native plugin installation/refresh across Codex, Claude Code, and Antigravity,
    optional non-destructive `~/.config/agroute/config.toml` reserve defaults, and
    `--dry-run` planning.
  - `gardenia repo check`, `gardenia repo init`, and `gardenia repo render` — explicit
    separation of tracked repository settings (`AGENTS.md` managed block,
    `CLAUDE.md -> AGENTS.md` symlink, `gardenia.toml` / `[tool.gardenia]`, optional
    `howhard.toml` and `agroute.toml`) from local per-clone `.git/` state
    (`.git/howhard` non-destructive pre-4.0 loose JSON warning and `.git/agroute`
    pending price inspection via `agr prices pending --json`), with linked-worktree
    protection and surrounding-prose preservation.
  - `gardenia check` — unified host + repository diagnostic returning the
    `gardenia-check-v1` JSON schema with explicit `scope: "host" | "repo"` findings.
- Added geometric monochrome `docs/assets/gardenia.svg` mark with provenance in
  `docs/assets/README.md`.
- Added Read the Docs Sphinx + MyST + Furo documentation (`docs/quickstart.md`,
  `docs/host-vs-repo.md`, `docs/architecture.md`, `docs/reference.md`,
  `docs/contributing.md`, `docs/releasing.md`, `docs/changelog.md`).
