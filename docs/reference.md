# CLI and schema reference

`gardenia` ships a single console script named `gardenia` (equivalent to
`python -m gardenia`) with zero third-party runtime dependencies.

## Exit codes

| Exit code | Meaning |
| ---: | --- |
| `0` | Success; checks found no errors (and no warnings when `--strict` is set). |
| `1` | Check found one or more errors (or warnings under `--strict`), or a host setup command failed. |
| `2` | Invalid CLI usage, invalid configuration, or safety precondition refusal (for example: non-Git directory, linked worktree without `--allow-worktree`, `--broker user` against an active `cgroup-v2` enforced broker without `--force-broker`, or overwriting an existing `~/.config/agroute/config.toml` without `--overwrite-config`). |

## Global options

```sh
gardenia --version
```

Prints `gardenia 0.1.0` and exits `0`.

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

1. Host CLIs on `PATH`: `howhard` (`>= 4.0.0`), `agr` (`>= 0.6.0`), and on Linux `agc`
   (`>= 0.10.0`).
2. Linux `agcoord` broker status via `agc --json list` (bounded timeout), classifying
   `resource_bindings` into `cgroup-v2` (enforced) vs. `admission-only` (user mode).
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
| `--broker MODE` | `auto` (preserves active `cgroup-v2` enforced broker; installs user broker only when missing/unreachable), `user` (refuses with exit `2` if enforced broker active unless `--force-broker`), `enforced` (`agc host install --download` or `upgrade --download`), or `skip`. |
| `--force-broker` | Allow `--broker user` even when an active `cgroup-v2` enforced broker is detected. |
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
| `--tmpfs-gb FLOAT` | unset | Optional tmpfs claim in GiB (rendered as exact integer bytes when set). |
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
| `--dry-run` | Plan edits without writing files. |
| `--json` | Emit structured JSON output. |

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

## `gardenia-check-v1` JSON schema

```json
{
  "format": "gardenia-check-v1",
  "version": "0.1.0",
  "ok": true,
  "strict": false,
  "host": {
    "platform": "linux",
    "home": "/home/user",
    "detected_clients": ["codex", "claude", "agy"],
    "clis": {
      "howhard": {"package": "howhard", "binary": "howhard", "min_version": "4.0.0", "installed": true, "path": "/home/user/.local/bin/howhard", "version": "4.0.0", "status": "ok", "reason": null}
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
