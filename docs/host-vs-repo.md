# Host vs. repository boundary

Mixing workstation setup with repository configuration is the single most common source
of drift, broken CI, and accidentally committed machine paths in multi-agent setups.
`gardenia` enforces a strict three-zone model across its CLI, JSON schemas, and internal
modules:

1. **Per-Host (Workstation / OS User)** — managed exclusively by `gardenia host ...`.
2. **Per-Repository Tracked Policy (Committed in Git)** — managed by `gardenia repo init`
   / `gardenia repo render` and verified by `gardenia repo check`.
3. **Per-Repository Local Clone State (Untracked under `.git/`)** — inspected read-only
   by `gardenia repo check`, shared across linked Git worktrees through
   `git rev-parse --git-common-dir`, and never committed to Git.

## Side-by-side boundary matrix

| Artifact or Setting | Zone | Location | Managed / Checked By | Shared Across |
| --- | --- | --- | --- | --- |
| `howhard`, `agr`, `agc`, `gardenia` binaries | **Per-Host** | `~/.local/bin` (via `uv tool` / `pipx`) | `gardenia host setup` / `host check` | All repos on workstation |
| `agcoord` host broker & cgroup-v2 bindings | **Per-Host** | `~/.local/state/agcoord`, `~/.local/libexec/agcoord`, or `/usr/libexec/agcoord` | `gardenia host setup --broker ...` / `host check` | All repos & worktrees for OS user |
| User-scoped Codex plugins & cache | **Per-Host** | `~/.codex/config.toml`, `~/.codex/plugins/cache/<pkg>/<pkg>/*/plugin.json` | `gardenia host setup --clients codex` / `host check` | All Codex sessions |
| User-scoped Claude Code plugins | **Per-Host** | `~/.claude/plugins/` (`--scope user`) | `gardenia host setup --clients claude` / `host check` | All Claude Code sessions |
| User-scoped Antigravity plugins | **Per-Host** | `~/.gemini/config/plugins/` | `gardenia host setup --clients agy` / `host check` | All Antigravity sessions |
| Subscription plans, quota cutoffs & credit reserves | **Per-Host** | `${XDG_CONFIG_HOME:-~/.config}/agroute/config.toml` | `gardenia host setup --reserve-*` / `host check` | All repos for OS user |
| User-configured tools & plugins | **Per-Host** | `${XDG_CONFIG_HOME:-~/.config}/gardenia/config.toml` (or `GARDENIA_HOST_CONFIG`) | Read by `gardenia host`, `gardenia check`, `gardenia repos check`, and, for repos declaring `tools`, `repo init` / `repos upgrade` | All repos for OS user |
| Installed plugin `AGENTS.snippet.md` files | **Per-Host** | Each client's installed plugin root, or a local-directory plugin source | Read as above, never by `repo check` / `repo render` | All repos declaring the tool |
| Managed repository registry | **Per-Host** | `${XDG_CONFIG_HOME:-~/.config}/gardenia/repos.toml` (or `GARDENIA_REPOS_CONFIG`) | `gardenia repos` (`list`, `add`, `remove`, `scan`, `prune`) and primary `repo init` | All fleet sweeps for OS user |
| Upgrade sweep failure logs | **Per-Host** | `${XDG_STATE_HOME:-~/.local/state}/gardenia/logs/` (or `GARDENIA_LOG_DIR`) | `gardenia repos upgrade` | Local diagnostics only |
| Contributor guardrails (managed block + project rules) | **Per-Repo Tracked** | `<repo>/AGENTS.md` | `gardenia repo init` / `repo render` / `repo check` | All contributors via Git |
| Claude Code root instruction symlink | **Per-Repo Tracked** | `<repo>/CLAUDE.md -> AGENTS.md` | `gardenia repo init` / `repo check` | All contributors via Git |
| Gardenia repository profile | **Per-Repo Tracked** | `<repo>/gardenia.toml` (or `[tool.gardenia]` in `pyproject.toml`) | `gardenia repo init --save-config` / `repo check` | All contributors via Git |
| Per-tool sibling blocks (`<!-- <tool>:begin -->`) | **Per-Repo Tracked** | `<repo>/AGENTS.md` | Stamped by `repo init` / `repos upgrade`; presence and integrity checked by `repo check` | All contributors via Git |
| Stealth repository profile (`repo init --stealth`) | **Per-Repo Local `.git/`** | `<git-common-dir>/gardenia/gardenia.toml` | `gardenia repo init` / `repo check` | Primary checkout + all linked worktrees of one clone |
| Optional project `howhard` policy/inventories | **Per-Repo Tracked** | `<repo>/howhard.toml` | Validated by `gardenia repo check` | All contributors via Git |
| Optional project `agroute` overrides | **Per-Repo Tracked** | `<repo>/agroute.toml` | Validated by `gardenia repo check` | All contributors via Git |
| `howhard` learned store, payloads, & semantic memory | **Per-Repo Local `.git/`** | `<git-common-dir>/howhard/` (`store.json`, `payloads.sqlite3`, `memory.sqlite3`) | Inspected read-only by `gardenia repo check` | Primary checkout + all linked worktrees of one clone |
| `agroute` route history, usage, & resolved prices | **Per-Repo Local `.git/`** | `<git-common-dir>/agroute/` (`routes.sqlite3`, `prices.json`, `usage.json`) | Inspected read-only by `gardenia repo check` (`agr prices pending --json`) | Primary checkout + all linked worktrees of one clone |

## Custom and private tools

The built-in stack (`agrein`, `agroute`, `howhard`, `agcoord`) can be extended with your own
CLI packages and agent plugins, including private ones. Which tools exist and where they
install from is **host** configuration. Which tools a repository follows is **repository**
configuration. Each tool stays standalone: it needs no `gardenia` dependency or callback,
only an optional static file. The full schema is in the
[CLI and schema reference](reference.md#host-tool-and-plugin-configuration-configtoml).

The example below uses a private merge-gate toolkit named `hotgate`.

### 1. Register the tool on each host

`${XDG_CONFIG_HOME:-~/.config}/gardenia/config.toml`:

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

- `gardenia host setup` installs or upgrades `hotgate` with `uv` or `pipx` from the private
  SSH repository, and registers its plugin with each listed client. Git authentication is
  whatever your SSH or Git credential setup already provides; do not put tokens in URLs.
- `gardenia host check` verifies `hotgate --version >= 0.1.0`, verifies plugin installation
  and enablement per client, and reports CLI-vs-plugin version drift under
  `"scope": "host"`.

### 2. Ship `AGENTS.snippet.md` from the plugin

A plugin that wants repository instructions ships a plain, static `AGENTS.snippet.md` at its
root, for example:

```markdown
- Run `hotgate verify` before asking to land a change.
```

The file holds the block body only. It may optionally be wrapped in its own
`<!-- hotgate:begin -->` / `<!-- hotgate:end -->` pair, but must contain no other
`<!-- <name>:begin -->` or `<!-- <name>:end -->` marker, and must not be empty.

Claude Code and Codex install a plugin as `<name>@<name>`, so the marketplace name in the
plugin repository must equal the plugin `name`.

### 3. Opt a repository in

`gardenia.toml` (or `[tool.gardenia]`):

```toml
gate = "bash scripts/check"
cpu = 2
memory_gb = 4
tools = ["hotgate"]
```

`gardenia repo init` and `gardenia repos upgrade` then write or refresh this sibling block,
leaving all other content in `AGENTS.md` alone:

```markdown
<!-- hotgate:begin -->
- Run `hotgate verify` before asking to land a change.
<!-- hotgate:end -->
```

Removing a name from `tools` does not delete its block; delete it by hand.

### What each check reads

| Command | `gardenia` managed block | Sibling blocks | Host installation |
| --- | --- | --- | --- |
| `gardenia repo check` | Exact match against the profile | Present, well formed, non-overlapping | Never read |
| `gardenia check`, `gardenia repos check` | Same | Same, plus content compared with the installed snippet | Verified for the declared tools |

`repo render` and `repo check` never read the host `config.toml` or any plugin directory, so
CI and machines without a private plugin still pass `repo check`.

### External or shared repositories: stealth profiles

When a repository must not receive `gardenia.toml` or any managed marker, keep the profile
local to your clone:

```sh
gardenia repo init --stealth --tool hotgate --gate "make test"
gardenia repo check
gardenia check
```

- Only `<git-common-dir>/gardenia/gardenia.toml` is written. `AGENTS.md`, `CLAUDE.md`, and
  the working tree are never modified, so nothing can reach `origin`.
- Once the file exists, later `repo init` runs and `repos upgrade` policy sweeps stay in
  stealth mode. `repo init` may run in a linked worktree because it changes no tracked file.
- `repo check` does not require `AGENTS.md`, `CLAUDE.md`, or markers, and warns if
  `AGENTS.md` contains `gardenia` or declared-tool markers. `gardenia check` and
  `repos check` still verify that the host has each declared tool installed.
- A tracked `gardenia.toml` or `[tool.gardenia]` always takes precedence: `--stealth` is
  refused when one exists, and a leftover stealth file is reported as ignored.
- To switch to a tracked profile, delete the stealth file and run `repo init --save-config`.

Stealth mode governs policy files only. `repos upgrade --land` still opens an issue and a
pull request if dependency lock upgrades change tracked files; pass `--no-upgrade-locks` or
omit `--land` for repositories where that is unwanted.

## Why these boundaries are enforced in code

### 1. Protecting enforced `cgroup-v2` brokers

On Ubuntu 24.04-class hosts with a privileged `cgroup-v2` AGCoord broker, blindly running
`agc host install --user` would replace or conflict with kernel-enforced resource limits.
`gardenia host check` probes `agc --json list --limit 1` (`resource_bindings`) read-only
with a `30s` timeout so large historical run logs do not bloat IPC responses; if the probe
still times out under heavy load, it reports `reason: "timeout"` with a non-destructive
`agc --json list --limit 1` remediation rather than suggesting a user-mode broker install.
`gardenia host setup`:

- Preserves any active `cgroup-v2` enforced broker untouched under `--broker auto`.
- Refuses to mutate the AGCoord broker (`--broker auto`, `user`, or `enforced`) with exit
  code `2` when the `agc --json list --limit 1` probe times out, because `cgroup-v2` vs.
  `admission-only` mode cannot be determined safely (retry when host load subsides, pass
  `--broker skip`, or pass `--force-broker`).
- Refuses `--broker user` with exit code `2` when an enforced broker is detected (unless
  `--force-broker` is explicitly supplied).
- Uses `agc host upgrade --download` (or `agc host install --download`) only when the
  operator explicitly asks for `--broker enforced`.

### 2. Non-interactive Codex inspection

Invoking `codex plugin list` without a TTY opens an interactive TUI and blocks automation.
`gardenia host check` never invokes `codex plugin list`. Instead, it parses
`~/.codex/config.toml` (`[marketplaces.<pkg>]` and `[plugins."<pkg>@<pkg>"]`) and reads
cached plugin manifests at `~/.codex/plugins/cache/<pkg>/<pkg>/*/plugin.json` (selecting
the highest semantic version when multiple cached versions exist, falling back to
`~/.codex/.tmp/marketplaces/<pkg>/plugin.json`).

### 3. Linked worktrees vs. primary checkouts

Under the `agrein` workflow, ticket work happens in temporary linked Git worktrees
(for example `/var/tmp/<repo>-<N>-wt`) while the primary checkout stays clean.

- `gardenia repo init` refuses linked worktrees by default (exit code `2`) so routine
  onboarding runs in the primary checkout rather than an ephemeral ticket branch; pass
  `--allow-worktree` when intentionally updating `AGENTS.md` inside an issue worktree.
  Even with `--allow-worktree`, `repo init` automatically skips updating
  `repos.toml` (and refuses explicit `--register` with exit code `2`) so ephemeral
  `/var/tmp` issue worktrees never pollute the host repository registry.
- `gardenia repo check` resolves both `--git-dir` and `--git-common-dir` so it accurately
  locates shared `.git/howhard` and `.git/agroute` stores from any linked worktree.
- `gardenia repos upgrade` never mutates any registered repository's primary checkout:
  each repository is evaluated inside a temporary isolated worktree created under
  `--worktree-root` (default `/var/tmp`) and removed in `finally`, with failure logs
  persisted under host state (`${XDG_STATE_HOME:-~/.local/state}/gardenia/logs/`).

### 4. Non-destructive `howhard` legacy payload checks

If a repository's `<git-common-dir>/howhard/` still contains pre-4.0 loose
`requests/*.json` or `decisions/*.json` files, `gardenia repo check` emits a
`scope="repo"` warning pointing to `howhard`'s `migrate-payloads.py` script without ever
deleting or rewriting those files.

### 5. Scope-tagged diagnostics

When you run `gardenia check --json`, every finding explicitly declares
`"scope": "host"` or `"scope": "repo"`. Automation and humans can tell immediately
whether a failure needs a workstation fix (`gardenia host setup`) or a tracked
repository update (`gardenia repo init`).
