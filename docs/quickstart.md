# Quickstart

`gardenia` splits onboarding into two clean phases so workstation state never leaks
into repository commits and repository edits never touch your home directory.

## Prerequisites

- Python 3.11 or newer.
- `uv` (recommended) or `pipx` for installing user-scoped Python CLIs.
- Git, plus at least one supported coding-agent client (`codex`, `claude`, or `agy`).
- Linux on `x86_64` for `agcoord` (`agc`); on macOS or other non-Linux platforms,
  `gardenia` automatically skips `agcoord` checks unless `--with-agcoord` is passed.

Install `gardenia` once on your workstation:

```sh
uv tool install gardenia
gardenia --version
```

Expect `gardenia 0.2.5`.

## Step 1 — Once per workstation (`gardenia host`)

Inspect what is currently installed on your host without modifying anything:

```sh
gardenia host check
```

Preview the exact installation plan (CLIs, Linux `agc` broker, user-scoped plugins across
your detected clients, and optional `agroute` host quota reserves) before running it:

```sh
gardenia host setup --dry-run
```

Apply host setup:

```sh
gardenia host setup \
  --reserve-pct 5 \
  --host-reserve-credit 20 \
  --min-remaining-credit 5
```

What `gardenia host setup` does:

1. Installs `howhard` (`>= 6.0.0`), `agroute` (`>= 0.7.0`), and (on Linux) `agcoord`
   (`>= 0.10.0`) via `uv tool install` (or `pipx install` fallback). Pass `--upgrade` to
   upgrade already-installed tools.
2. Configures the Linux `agcoord` broker safely:
   - Default `--broker auto` preserves an active `cgroup-v2` enforced broker untouched
     and never downgrades it with `--user`. When no broker is reachable yet, it runs
     `agc host install --user` (no root required).
   - Pass `--broker enforced` on a supported Ubuntu 24.04-class host to run
     `agc host install --download` (or `agc host upgrade --download` with `--upgrade`).
3. Installs or updates the four user-scoped native plugins (`agrein`, `agcoord`, `howhard`,
   `agroute`) for each detected client (or the subset named by `--clients codex,claude,agy`).
4. When any reserve flag is supplied, writes `~/.config/agroute/config.toml` across
   `[agents.codex]`, `[agents.claude]`, and `[agents.agy]`, refusing to overwrite an
   existing file unless `--overwrite-config` is passed.

Verify host health (read-only):

```sh
gardenia host check --strict
```

## Step 2 — Once per repository (`gardenia repo`)

Change into your repository's **primary checkout** (not a linked Git worktree). Preview
the managed guardrail block first if you want to review what will be written:

```sh
gardenia repo render --gate "bash scripts/check" --cpu 4 --memory-gb 4
```

Initialize the repository and save its profile to `gardenia.toml`:

```sh
gardenia repo init \
  --gate "bash scripts/check" \
  --cpu 4 \
  --memory-gb 4 \
  --routing agroute \
  --agcoord auto \
  --save-config
```

What `gardenia repo init` does:

1. Inserts or updates **only** the delimited
   `<!-- gardenia:begin --> ... <!-- gardenia:end -->` region in `AGENTS.md`, leaving
   every project-specific line before and after those markers completely untouched.
2. Creates a relative `CLAUDE.md -> AGENTS.md` symlink (skipping with a warning if
   `CLAUDE.md` is already a non-matching regular file unless `--force-claude-symlink` is
   passed).
3. With `--save-config`, writes `gardenia.toml` so future `gardenia repo check` and
   `gardenia repo init` runs share the same profile.
4. Automatically records the primary checkout in `${XDG_CONFIG_HOME:-~/.config}/gardenia/repos.toml`
   (skipped automatically inside linked Git worktrees or when `--no-register` is passed).

Verify repository tracked policy and local `.git/` state:

```sh
gardenia repo check --strict
```

## Step 3 — Across your workstation fleet (`gardenia repos`)

Discover already-managed repositories under your workspace root, inspect fleet status,
and run read-only checks across every registered repository:

```sh
gardenia repos scan --root ~/git
gardenia repos list --strict
gardenia repos check --strict
```

When `gardenia`, `pytest`, `howhard`, `agroute`, or another dependency upgrades, sweep
every managed repository safely inside disposable isolated Git worktrees (leaving every
primary checkout untouched):

```sh
gardenia repos upgrade --dry-run
gardenia repos upgrade --package howhard --file-issues
```

Pass `--land` to commit verified policy/lockfile updates on a dedicated branch, open a
tracking issue and pull request, and land through `agc land` (or `gh pr merge` when only
documentation-exempt files changed).

For a policy-only sweep, pass `--no-upgrade-locks`. Repositories that are already in sync,
or whose only changes qualify under `docs_exemption`, skip the gate; the rest still run it
with a provisioned test virtualenv, without dependency upgrades. See
`gardenia repos upgrade` in the [CLI reference](reference.md) for the exact rules.

## Unified check

Run both scopes together from any checkout (or add `--repo PATH`):

```sh
gardenia check
gardenia check --json
```

Every finding in `gardenia check` explicitly carries `"scope": "host"` or
`"scope": "repo"` along with a concrete remediation command.

## Live terminal UIs

Watch live agent routing and job execution without running internal agent commands:

```sh
agr tui   # model routing and subscription usage limits
agc tui   # machine job queue, CPU/memory/tmpfs usage, and merge queue
```
