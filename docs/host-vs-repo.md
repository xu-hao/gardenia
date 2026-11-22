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
| Contributor guardrails (managed block + project rules) | **Per-Repo Tracked** | `<repo>/AGENTS.md` | `gardenia repo init` / `repo render` / `repo check` | All contributors via Git |
| Claude Code root instruction symlink | **Per-Repo Tracked** | `<repo>/CLAUDE.md -> AGENTS.md` | `gardenia repo init` / `repo check` | All contributors via Git |
| Gardenia repository profile | **Per-Repo Tracked** | `<repo>/gardenia.toml` (or `[tool.gardenia]` in `pyproject.toml`) | `gardenia repo init --save-config` / `repo check` | All contributors via Git |
| Optional project `howhard` policy/inventories | **Per-Repo Tracked** | `<repo>/howhard.toml` | Validated by `gardenia repo check` | All contributors via Git |
| Optional project `agroute` overrides | **Per-Repo Tracked** | `<repo>/agroute.toml` | Validated by `gardenia repo check` | All contributors via Git |
| `howhard` learned store, payloads, & semantic memory | **Per-Repo Local `.git/`** | `<git-common-dir>/howhard/` (`store.json`, `payloads.sqlite3`, `memory.sqlite3`) | Inspected read-only by `gardenia repo check` | Primary checkout + all linked worktrees of one clone |
| `agroute` route history, usage, & resolved prices | **Per-Repo Local `.git/`** | `<git-common-dir>/agroute/` (`routes.sqlite3`, `prices.json`, `usage.json`) | Inspected read-only by `gardenia repo check` (`agr prices pending --json`) | Primary checkout + all linked worktrees of one clone |

## Why these boundaries are enforced in code

### 1. Protecting enforced `cgroup-v2` brokers

On Ubuntu 24.04-class hosts with a privileged `cgroup-v2` AGCoord broker, blindly running
`agc host install --user` would replace or conflict with kernel-enforced resource limits.
`gardenia host check` inspects `agc --json list` (`resource_bindings`) read-only, and
`gardenia host setup`:

- Preserves any active `cgroup-v2` enforced broker untouched under `--broker auto`.
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
- `gardenia repo check` resolves both `--git-dir` and `--git-common-dir` so it accurately
  locates shared `.git/howhard` and `.git/agroute` stores from any linked worktree.

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
