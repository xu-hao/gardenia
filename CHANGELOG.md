# Changelog

All notable changes to `gardenia` are documented in this file.

## Unreleased

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
