# CLI and schema reference

`gardenia` ships a single console script named `gardenia` (equivalent to
`python -m gardenia`) with zero third-party runtime dependencies.

## Exit codes

| Exit code | Meaning |
| ---: | --- |
| `0` | Success; checks found no errors (and no warnings when `--strict` is set). |
| `1` | Check found one or more errors (or warnings under `--strict`), or a host setup command failed. |
| `2` | Invalid CLI usage, invalid configuration, or safety precondition refusal (for example: non-Git directory, linked worktree without `--allow-worktree`, `--broker user` against an active `cgroup-v2` enforced broker without `--force-broker`, broker mutation when the `agc --json list --limit 1` probe timed out without `--broker skip` or `--force-broker`, or overwriting an existing `~/.config/agroute/config.toml` without `--overwrite-config`). |

## Global options

```sh
gardenia --version
```

Prints `gardenia 0.2.5` and exits `0`.

## `gardenia host check`

Read-only workstation inspection. Never mutates files, installs packages, or starts
daemons.

```sh
gardenia host check [--clients codex,claude,agy] [--with-agcoord] [--strict] [--json]
```

| Flag | Description |
| --- | --- |
| `--clients LIST` | Comma-separated subset of `codex`, `claude`, `agy` (default: auto-detect clients on `PATH`). |
| `--with-agcoord` | Inspect `agc` and its broker even on non-Linux platforms (otherwise skipped as `platform-unsupported`). |
| `--strict` | Exit `1` on warnings (such as CLI-vs-plugin version drift) as well as errors. |
| `--json` | Emit `gardenia-check-v1` JSON output. |

Checks performed:

1. Host CLIs on `PATH`: `howhard` (`>= 6.0.0`), `agr` (`>= 0.7.0`), and on Linux `agc`
   (`>= 0.10.0`).
2. Linux `agcoord` broker status via `agc --json list --limit 1` (`30s` timeout),
   classifying `resource_bindings` into `cgroup-v2` (enforced) vs. `admission-only`
   (user mode) and distinguishing probe timeouts (`reason: "timeout"`, non-destructive
   `agc --json list --limit 1` remediation) from missing or stopped brokers.
3. User-scoped plugins (`agrein`, `agcoord`, `howhard`, `agroute`) per target client:
   - `agy`: `agy plugin list --json`.
   - `claude`: `claude plugin list --json`.
   - `codex`: parses `~/.codex/config.toml` (`[marketplaces.<pkg>]` and
     `[plugins."<pkg>@<pkg>"]`) and reads `~/.codex/plugins/cache/<pkg>/<pkg>/*/plugin.json`
     (selecting the highest semver, falling back to
     `~/.codex/.tmp/marketplaces/<pkg>/plugin.json`). Never runs `codex plugin list`.
4. CLI-vs-plugin version drift (`howhard`, `agroute`, `agcoord`) and disabled/missing
   plugins.
5. Host `agroute` reserve configuration at `${XDG_CONFIG_HOME:-~/.config}/agroute/config.toml`
   (or `AGROUTE_USER_CONFIG` when set): TOML syntax, numeric bounds, and per-agent
   summary (`plan`, `enabled`, `host_reserve_pct`, `host_reserve_credit_usd`,
   `min_remaining_credit_usd`, `max_session_used_pct`, `max_weekly_used_pct`,
   `max_tier_used_pct`).

## `gardenia host setup`

Idempotent workstation bootstrapper and upgrader. Validates all safety preconditions
before executing any mutation.

```sh
gardenia host setup \
  [--clients codex,claude,agy] \
  [--installer auto|uv|pipx] \
  [--broker auto|user|enforced|skip] \
  [--force-broker] \
  [--upgrade] \
  [--reserve-pct FLOAT] \
  [--host-reserve-credit FLOAT] \
  [--min-remaining-credit FLOAT] \
  [--overwrite-config] \
  [--with-agcoord] \
  [--dry-run] \
  [--json]
```

| Flag | Description |
| --- | --- |
| `--clients LIST` | Comma-separated clients to configure (default: detected clients on `PATH`). |
| `--installer MODE` | `auto` (prefers `uv`, falls back to `pipx`), `uv`, or `pipx`. |
| `--broker MODE` | `auto` (preserves active `cgroup-v2` enforced broker; installs user broker only when confirmed missing/stopped; refuses with exit `2` if broker probe times out unless `--force-broker`), `user` (refuses with exit `2` if enforced broker active or probe times out unless `--force-broker`), `enforced` (`agc host install --download` or `upgrade --download`; refuses with exit `2` if probe times out unless `--force-broker`), or `skip`. |
| `--force-broker` | Allow `--broker user` over an active `cgroup-v2` enforced broker, or override broker mutation refusal when the `agc --json list --limit 1` probe times out. |
| `--upgrade` | Upgrade already-installed CLIs and refresh plugins across target clients. |
| `--reserve-pct FLOAT` | Write `host_reserve_pct` (`0..100`) for `codex`, `claude`, and `agy` in `~/.config/agroute/config.toml`. |
| `--host-reserve-credit FLOAT` | Write `host_reserve_credit_usd` (`>= 0`) for each agent. |
| `--min-remaining-credit FLOAT` | Write `min_remaining_credit_usd` (`>= 0`) for each agent. |
| `--overwrite-config` | Permit replacing an existing `~/.config/agroute/config.toml`. |
| `--dry-run` | Return the ordered action plan without executing commands or writing files. |
| `--json` | Emit structured JSON result including the ordered `actions` list. |

## `gardenia repo check`

Read-only repository diagnostic separating tracked policy from local per-clone `.git/`
state. Exits `2` if the target directory is not inside a Git repository.

```sh
gardenia repo check [--repo PATH] [--strict] [--json]
```

Reports:

- `repo.tracked`: `root`, `is_linked_worktree`, `config` (`gardenia.toml`, `[tool.gardenia]`,
  or `defaults`), `agents_md` (presence, managed block markers, drift vs. rendered block,
  referenced layers), `claude_md` (`symlink_to_agents`, `symlink_other`, `regular_file`,
  `missing`), and optional `howhard.toml` / `agroute.toml` TOML validity.
- `repo.local_state`: `git_dir`, `git_common_dir`, `howhard_store` (`store.json`,
  `payloads.sqlite3`, and non-destructive counts of legacy loose `requests/*.json` /
  `decisions/*.json`), and `agroute_store` (directory presence and non-mutating
  `agr prices pending --json` check when `agr` is on `PATH`).

## `gardenia repo init` & `gardenia repo render`

```sh
gardenia repo init [OPTIONS]
gardenia repo render [OPTIONS]
```

Shared profile options:

| Flag | Default | Description |
| --- | --- | --- |
| `--repo PATH` | current Git top-level | Target Git repository (`repo render` also works outside a Git repo using defaults when `--repo` is omitted). |
| `--gate CMD` | `bash scripts/check` | Full validation command for checks and landing. |
| `--cpu INT` | `4` | CPU claim for `agc run` / `agc land`. |
| `--memory-gb FLOAT` | `4.0` | Memory claim in GiB (rendered as exact integer bytes for `agc --resource memory=<bytes>`). |
| `--tmpfs-gb FLOAT` | unset | Optional tmpfs claim in GiB (rendered as exact integer bytes alongside `--resource tmpfs_inodes=262144` when set). |
| `--no-overflow` / `--allow-overflow` | `--no-overflow` | Include or omit `--no-overflow` in `agc` commands. |
| `--routing MODE` | `agroute` | `agroute` (full 5-step `agr` + `howhard` delegation), `howhard` (native `howhard` only), or `none`. |
| `--agcoord MODE` | `auto` | `auto` (use `agc` on Linux, direct gate elsewhere), `required`, or `none`. |
| `--docs-exemption` / `--no-docs-exemption` | enabled | Include or omit the documentation-only exemption clause (aliases: `--docs-only-exemption` / `--no-docs-only-exemption`). |
| `--claude-symlink` / `--no-claude-symlink` | enabled | Manage `CLAUDE.md -> AGENTS.md`. |

Additional `gardenia repo init` options:

| Flag | Description |
| --- | --- |
| `--force-claude-symlink` | Replace a non-matching regular file or symlink at `CLAUDE.md`. |
| `--save-config` | Write the resolved profile cleanly to `gardenia.toml`. |
| `--allow-worktree` | Allow running `repo init` inside a linked Git worktree (otherwise refused with exit `2`). |
| `--register` / `--no-register` | Control updating the host repository registry (`repos.toml`). Defaults to registering primary checkouts and automatically skipping linked worktrees; explicit `--register` on a linked worktree is refused with exit `2`. |
| `--dry-run` | Plan edits without writing files. |
| `--json` | Emit structured JSON output. |

## `gardenia repos`

Multi-repository workstation registry, fleet verification, and isolated-worktree policy
and dependency upgrade sweeps.

The host repository registry path resolves as:

1. `$GARDENIA_REPOS_CONFIG` (when set and non-empty), else
2. `${XDG_CONFIG_HOME}/gardenia/repos.toml` (when `XDG_CONFIG_HOME` is set), else
3. `${HOME}/.config/gardenia/repos.toml`.

### Registry subcommands

```sh
gardenia repos list [--root PATH] [--max-depth INT] [--strict] [--json]
gardenia repos add [PATH ...] [--json]
gardenia repos remove PATH [PATH ...] [--json]
gardenia repos scan --root PATH [--max-depth INT] [--dry-run] [--json]
gardenia repos prune [--dry-run] [--json]
```

- `repos list`: Inspects every registered repository (plus non-mutating discovery under
  `--root` when provided) and reports `path`, `name`, `registered`, `exists`,
  `is_git_repo`, `is_linked_worktree`, `branch`, `clean`, `managed`, and
  `agents_md_in_sync` in `gardenia-repos-list-v1` JSON. Exits `0` (or `1` under
  `--strict` if any listed repository is missing, not a primary Git repo, unmanaged, or
  has `agents_md_in_sync: false`).
- `repos add`: Resolves each path (defaulting to the current working directory) to its
  primary Git root, refusing non-Git directories and linked worktrees with exit `2`, and
  atomically updates `repos.toml`.
- `repos remove`: Canonicalizes each path and removes matching entries from `repos.toml`
  idempotently.
- `repos scan`: Walks `--root` up to `--max-depth` (default `2`, must be `>= 0`),
  skipping hidden directories, `node_modules`, `__pycache__`, and linked worktrees, and
  registers primary Git checkouts managed by `gardenia` (`gardenia.toml`,
  `[tool.gardenia]` in `pyproject.toml`, or `<!-- gardenia:begin -->` in `AGENTS.md`).
- `repos prune`: Removes entries from `repos.toml` that no longer exist on disk or are
  no longer primary Git checkouts.

### `gardenia repos check`

Runs read-only `gardenia repo check` across all target repositories (explicit repeatable
`--repo PATH`, or `repos.toml` combined with optional `--root` discovery; exits `2` when
no target repositories are found). Missing or broken registered paths are captured as
repo-level error entries rather than aborting the fleet check.

```sh
gardenia repos check [--root PATH] [--repo PATH ...] [--max-depth INT] [--strict] [--json]
```

Emits `gardenia-repos-check-v1` JSON and exits `0` when all repositories pass or `1`
when any repository fails.

### `gardenia repos upgrade`

Sweeps target repositories inside disposable isolated Git worktrees without ever
mutating any primary checkout.

```sh
gardenia repos upgrade \
  [--root PATH] \
  [--repo PATH ...] \
  [--max-depth INT] \
  [--package PKG ...] \
  [--sync-policy | --no-sync-policy] \
  [--upgrade-locks | --no-upgrade-locks] \
  [--land] \
  [--file-issues] \
  [--gate-timeout SECONDS] \
  [--worktree-root DIR] \
  [--log-dir DIR] \
  [--dry-run] \
  [--json]
```

| Flag | Default | Description |
| --- | --- | --- |
| `--root PATH` | unset | Optional workspace root to include in discovery when `--repo` is not passed. |
| `--repo PATH` | unset | Explicit target repository path (repeatable; overrides `repos.toml`). |
| `--package PKG` | unset | Specific package to upgrade (repeatable; validated against whitespace and shell metacharacters). |
| `--sync-policy` / `--no-sync-policy` | enabled | Refresh `AGENTS.md` / `CLAUDE.md` via `init_repo(..., allow_worktree=True, register=False)` inside the temporary worktree. |
| `--upgrade-locks` / `--no-upgrade-locks` | enabled | Refresh `uv.lock` (`uv lock --upgrade` / `--upgrade-package`) and `Cargo.lock` (`cargo update` / `-p`), and upgrade test-virtualenv dependencies. `--no-upgrade-locks` disables those upgrades only; it does not disable virtualenv provisioning, and with `--sync-policy` it enables the gate bypass described below. |
| `--land` | disabled | Push the verified candidate commit on `gardenia-upgrade-<timestamp>` to `origin`, open a tracking issue and PR (`Closes #<N>`), and land via `agc land` (or `gh pr merge --merge --delete-branch` when all changed files qualify under `docs_exemption` or `agc` is not active). |
| `--file-issues` | disabled | When a repository gate fails, open a GitHub issue via `gh` (or reuse an existing open issue with the same upgrade-failure title). |
| `--gate-timeout SECONDS` | `7200.0` | Timeout in seconds for each repository validation gate and `agc land` invocation (must be finite and `> 0`). |
| `--worktree-root DIR` | `/var/tmp` (or temp dir) | Base directory for temporary isolated worktrees (cleaned up unconditionally in `finally`). |
| `--log-dir DIR` | `$GARDENIA_LOG_DIR` or `${XDG_STATE_HOME:-~/.local/state}/gardenia/logs` | Directory where failed gate logs are persisted. |
| `--dry-run` | disabled | Inspect target repositories read-only and report planned steps without creating worktrees or running gates. Changed files are unknown at plan time, so the plan always lists the gate even if the real run may bypass it. |
| `--json` | disabled | Emit `gardenia-repos-upgrade-v1` JSON. |

Virtual environment provisioning for `pyproject.toml` repositories without `uv.lock`:

- Provisioning happens whenever the gate runs, with or without `--upgrade-locks`. Under `--no-upgrade-locks`, no install command receives `--upgrade` and the separate `--upgrade <PKG ...>` step for `--package` is skipped.
- Temporary virtual environments are created with `uv venv --seed <venv>` so `python -m pip` is available inside repository gate commands.
- When `[project]` is present in `pyproject.toml`, `gardenia` collects every present key among `("test", "docs", "dev")` in `[project.optional-dependencies]` and installs `-e ".[<extras>]"` (or `-e .` when none are defined), together with any requirements from `[dependency-groups]` (`dev`, `test`, `docs`, resolving `{"include-group": ...}` recursively) and `-r docs/requirements.txt` if present and `"docs"` was not already in `[project.optional-dependencies]`.
- When `[project]` is absent from `pyproject.toml` (PEP 735 tool/plugin repositories), editable installation (`-e .`) is skipped; instead, requirements from present `("dev", "test", "docs")` entries in `[dependency-groups]` (plus `-r docs/requirements.txt` if present) are installed with `uv pip install --python <venv_python>`, adding `--upgrade` only under `--upgrade-locks`.
- When `uv` is not on `PATH`, the equivalent `python -m venv` and `pip install` commands are used.

#### Gate bypass under `--no-upgrade-locks`

When `--no-upgrade-locks` is combined with `--sync-policy` (the default policy setting), the
gate, `agc run`, and virtualenv provisioning are skipped, and `used_agcoord` is `false`, in
two cases. `agcoord = "required"` does not require `agc` for a skipped gate.

| Condition after policy sync | Result | `upgrade_actions` entry |
| --- | --- | --- |
| No files changed | `up_to_date` | `skip_gate:up_to_date` |
| `docs_exemption = true` and every changed file is documentation-exempt | `changes_verified`, or with `--land`, `landed` via `gh_pr_merge_docs_exemption` | `skip_gate:docs_exemption` |

A `skip_gate:docs_exemption` result means the documentation-only exemption applied, not
that the gate passed. In every other case the gate runs: changes that do not qualify (including any
drift when `docs_exemption = false`), a gate-only sweep with
`--no-sync-policy --no-upgrade-locks`, and every run with `--upgrade-locks`.

#### Clean candidate checkout

- `changed_files` is recorded from `git status --porcelain` inside the isolated worktree after policy sync and lockfile upgrades, **before** virtualenv provisioning and the validation gate.
- When the gate runs and `changed_files` is not empty, only those files are committed (`git add -- ...`) to a local `gardenia-upgrade-<timestamp>` branch **before** the gate starts. This happens with or without `--land`, so the repository's commit hooks run in both cases. If no Git `user.name` or `user.email` is configured, `gardenia` supplies `gardenia` / `gardenia@localhost` for that commit.
- Immediately before the gate, anything virtualenv provisioning left in the source tree is discarded (modified tracked files, plus untracked files that are not ignored), so gates that require a clean checkout pass.
- After the gate succeeds, its modifications to tracked files, staged entries, and untracked files that are not ignored are discarded before any `--land` push or PR. `--land` pushes the candidate branch; when the gate was bypassed, the commit is created at this point.
- The temporary worktree and the local candidate branch are removed in `finally`. Without `--land`, nothing is pushed.

Per-repository statuses in `gardenia-repos-upgrade-v1`:

- `planned` (`--dry-run` only), `up_to_date`, `changes_verified`, `landed` → healthy (exit `0` when all repos succeed).
- `gate_failed`, `error` → failure (exit `1`; failure log persisted under `--log-dir` / `XDG_STATE_HOME`).

## `gardenia check`

Unified diagnostic running `host check` plus `repo check` when inside a Git repository
or when `--repo PATH` is provided.

```sh
gardenia check [--repo PATH] [--clients codex,claude,agy] [--with-agcoord] [--strict] [--json]
```

## Tracked configuration schema (`gardenia.toml` / `[tool.gardenia]`)

`gardenia` reads `gardenia.toml` at the repository root first, falling back to
`[tool.gardenia]` in `pyproject.toml`. Unknown keys or invalid types are rejected.

```toml
gate = "bash scripts/check"
cpu = 4
memory_gb = 4
# tmpfs_gb = 2
no_overflow = true
routing = "agroute"      # "agroute" | "howhard" | "none"
agcoord = "auto"         # "auto" | "required" | "none"
docs_exemption = true
claude_symlink = true
```

## Host repository registry schema (`repos.toml`)

```toml
format = "gardenia-repos-v1"
repos = [
  "/abs/path/to/repo-a",
  "/abs/path/to/repo-b",
]
```

Paths are canonicalized, deduplicated, sorted lexicographically, and written atomically
via a sibling temporary file plus `os.replace`. Malformed TOML, unknown keys, wrong
`format`, or invalid `repos` entries fail with exit code `2`.

## `gardenia-check-v1` JSON schema

```json
{
  "format": "gardenia-check-v1",
  "version": "0.2.5",
  "ok": true,
  "strict": false,
  "host": {
    "platform": "linux",
    "home": "/home/user",
    "detected_clients": ["codex", "claude", "agy"],
    "clis": {
      "howhard": {"package": "howhard", "binary": "howhard", "min_version": "6.0.0", "installed": true, "path": "/home/user/.local/bin/howhard", "version": "6.0.0", "status": "ok", "reason": null}
    },
    "broker": {
      "status": "ok",
      "reachable": true,
      "mode": "cgroup-v2",
      "enforced": true
    },
    "clients": {},
    "agroute_config": {"path": "/home/user/.config/agroute/config.toml", "exists": true, "valid": true, "agents": {}}
  },
  "repo": {
    "tracked": {
      "root": "/path/to/repo",
      "is_linked_worktree": false,
      "config": {"source": "gardenia.toml", "valid": true, "values": {}},
      "agents_md": {"exists": true, "has_managed_block": true, "in_sync": true, "layers_referenced": {"agrein": true, "agroute": true, "howhard": true, "agcoord": true}},
      "claude_md": {"status": "symlink_to_agents", "target": "AGENTS.md"},
      "howhard_toml": {"exists": false, "valid": null},
      "agroute_toml": {"exists": false, "valid": null}
    },
    "local_state": {
      "git_dir": "/path/to/repo/.git",
      "git_common_dir": "/path/to/repo/.git",
      "howhard_store": {"exists": false, "store_json": false, "payloads_sqlite": false, "has_legacy_loose_json": false},
      "agroute_store": {"exists": false, "prices_pending_checked": true, "pending_price_count": 0, "pending_models": []}
    }
  },
  "findings": [
    {
      "scope": "host",
      "severity": "warning",
      "component": "agy:agcoord",
      "message": "Version drift in client 'agy' for 'agcoord': plugin is 0.9.1 while host CLI is 0.10.1.",
      "remediation": "agy plugin uninstall agcoord && agy plugin install https://github.com/xu-hao/agcoord"
    }
  ]
}
```
