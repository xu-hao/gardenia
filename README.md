# gardenia

<img src="docs/assets/gardenia.svg" alt="gardenia fractal mark" width="160" height="160">


[Read the documentation](https://gardenia.readthedocs.io/en/latest/) · [Ecosystem overview](docs/index.md) · [Full-stack architecture](docs/architecture.md)

`gardenia` is the zero-dependency bootstrapper, repository initializer, verifier, and
canonical full-stack guide for a five-tool coding-agent ecosystem: [**`agrein`**](https://github.com/xu-hao/agrein),
[**`agroute`**](https://github.com/xu-hao/agroute) (`agr`), [**`howhard`**](https://github.com/xu-hao/howhard),
[**`agcoord`**](https://github.com/xu-hao/agcoord) (`agc`), and **`gardenia`**.

## Why five focused tools instead of a monolith

Running coding agents across real repositories and multi-ticket queues breaks down in five
predictable ways, each at a different system boundary:

1. **Workspace and workflow chaos** — Agents edit your primary checkout directly, patch
   symptoms without a failing reproduction test, loop in subagent-of-subagent chains, or
   make unsettled product and live-state decisions without asking the owner → solved by
   **`agrein`**.
2. **Blind subscription and quota burnout** — Static model rules exhaust one client's
   session/weekly window or dollar credit reserve while sibling clients have headroom, or
   fail on unpriced models and cross-agent CLI handoffs → solved by **`agroute` (`agr`)**.
3. **Guessing task difficulty** — Frontier max-effort tokens get burned on trivial edits
   while subtle changes get under-sized, pass shallow checks, and escape into `main` as
   silent bugs without learning from past runs → solved by **`howhard`**.
4. **Workstation thrashing and unguarded merges** — Parallel agent test suites starve CPU,
   RAM, and `tmpfs` on one Linux box, or pull requests tested against stale `main` merge
   over one another → solved by **`agcoord` (`agc`)**.
5. **Host-versus-repository setup drift** — Four CLIs, a Linux `cgroup-v2` broker,
   user-scoped plugins across three clients, tracked `AGENTS.md` policy blocks, local
   `.git/` stores, and multi-repo fleets drift out of sync or leak home-directory paths
   into Git → solved by **`gardenia`**.

Keeping each concern in its own tool means contributor habits stay pure Markdown, quota
windows stay separate from difficulty estimation, OS kernel enforcement stays independent
of LLM providers, and workstation state never leaks into repository commits.

## Each tool at a glance

| Tool | Plain-English job & key entry points | Owns | Never touches |
| --- | --- | --- | --- |
| [**`agrein`**](https://github.com/xu-hao/agrein) (*“a grain”*) | Contributor & coordinator workflow skills: `ticket-delivery`, `epic-queue`, `bug-investigation`, `owner-escalation` | Pure-Markdown Agent Skills per client (`codex`, `claude`, `agy`) | Runtime daemons, mutable stores, model choice, quotas, or OS limits |
| [**`agroute`**](https://github.com/xu-hao/agroute) (`agr`, *“a root”*) | Live multi-client quota/reserve filtering, offline price resolution, and launch planning: `agr usage`, `agr candidates`, `agr prices resolve`, `agr plan` | `~/.config/agroute/config.toml`, optional `agroute.toml`, `<git-common-dir>/agroute/` | Network price fetches, task difficulty estimation, semantic memory, or spawning workers |
| [**`howhard`**](https://github.com/xu-hao/howhard) | Bayesian `(agent, model, effort)` cost/risk selection, outcome accounting, defect attribution, and learning: `howhard context`/`retrieve`/`decide` (or `pick`), `howhard observe`, `attribute-failure`, `howhard memory` | Optional `howhard.toml` and per-clone `<git-common-dir>/howhard/` beliefs, payloads, and semantic memory | Host subscription caps across clients, worker process launch, OS limits, or Git merges |
| [**`agcoord`**](https://github.com/xu-hao/agcoord) (`agc`, *“a gourd”*) | Shared Linux job queue, kernel `cgroup-v2` isolation, and atomic gate-plus-merge landing: `agc run`, `agc full`, `agc land` (integer byte `memory` and paired `tmpfs` + `tmpfs_inodes`) | `${XDG_STATE_HOME:-~/.local/state}/agcoord` queue/logs, `cgroup-v2` slices, atomic PR merge | Model selection, task semantics, or source-code edits |
| **`gardenia`** | Out-of-band provisioning, policy sync, fleet sweeps, and scope-tagged diagnostics: `gardenia host`, `gardenia repo`, `gardenia repos`, `gardenia check` | Host setup & `~/.config/gardenia/repos.toml`, `AGENTS.md` managed block + `CLAUDE.md` + `gardenia.toml`, read-only `.git/` checks | Live routing hot path, executing gates in place of `agc`, or mutating primary checkouts in fleet sweeps |

Specifically, `gardenia` enforces the **host vs. repository** split across four command
groups:

- **`gardenia host ...`** inspects or configures only workstation / OS-user state (`PATH`
  CLIs, Linux `agc` broker without downgrading `cgroup-v2` enforcement, user-scoped
  plugins across Codex/Claude Code/Antigravity, and `~/.config/agroute/config.toml`). It
  never inspects or mutates a Git repository.
- **`gardenia repo ...`** inspects, renders, or updates only a target Git repository,
  separating **tracked repository settings** (`AGENTS.md` managed block,
  `CLAUDE.md -> AGENTS.md` symlink, `gardenia.toml` / `[tool.gardenia]`, optional
  `howhard.toml` and `agroute.toml`) from **local per-clone `.git/` state**
  (`<git-common-dir>/howhard/` and `<git-common-dir>/agroute/`, checked read-only).
- **`gardenia repos ...`** maintains the workstation's host repository registry
  (`${XDG_CONFIG_HOME:-~/.config}/gardenia/repos.toml`), runs read-only fleet audits
  (`gardenia repos check`), and performs isolated `/var/tmp` worktree policy and
  dependency upgrade sweeps (`gardenia repos upgrade`) without ever touching any primary
  checkout.
- **`gardenia check`** combines `host check` and (when inside a Git checkout or given
  `--repo`) `repo check`, tagging every finding with `"scope": "host"` or
  `"scope": "repo"` and a concrete remediation command.

## How they work together on one ticket

1. **Triage & scope (`agrein`)** — Verify or open a `#N — ...` GitHub issue
   (`ticket-delivery`), sequence multi-ticket dependencies (`epic-queue`), and escalate
   unsettled product or live-operation approvals upfront (`owner-escalation`).
2. **Filter live quota & pricing (`agroute`)** — Refresh usage windows (`agr usage`) and
   emit currently affordable `<agent>/<model>` routes (`agr candidates`), resolving any
   newly discovered unpriced models offline once via `agr prices resolve`.
3. **Size difficulty & resolve the execution plan (`howhard` → `agr plan`)** — Select the
   cost-minimizing `(agent, model, effort)` cell within the defect-risk cap using an
   independent stateless baseline forecast and repository memory (`howhard context` /
   `retrieve` / `decide`, or `pick`), then convert it via `agr plan` into a native
   subagent call (`target_agent == host_agent`) or cross-agent CLI command
   (`target_agent != host_agent`).
4. **Implement directly in an isolated worktree (`agrein`)** — The assigned leaf worker
   never re-delegates recursively; it works inside a dedicated `/var/tmp` Git worktree
   branched from `origin/main` (primary checkout stays read-only), starting bug fixes with
   a failing behavioral test (`bug-investigation`).
5. **Validate under `cgroup-v2` and merge atomically (`agcoord`)** — Run checks via
   `agc run` with integer byte `memory` (and optional `tmpfs` + `tmpfs_inodes`) claims,
   open the pull request (`Closes #N`), and gate-plus-merge atomically with `agc land <pr>`
   (or forge merge for documentation-only changes) before cleaning up `/var/tmp`.
6. **Record outcome & update semantic memory (`howhard`)** — Record the real outcome and
   measured usage (`howhard observe` and `howhard usage attach` following
   `matched_receipt` or `raw_native`) and distill reusable lessons into
   `<git-common-dir>/howhard/` via `howhard memory` (`learn-context` / `learn`).

`gardenia` sits outside this runtime loop: run `gardenia host` once per workstation,
`gardenia repo` once per repository to wire the `AGENTS.md` contract, `gardenia check` to
catch drift, and `gardenia repos` to audit or upgrade your repository fleet. Read the full
plain-English guide in [docs/index.md](docs/index.md) and the Coordinator vs. Leaf Worker
protocol in [docs/architecture.md](docs/architecture.md).

## Quickstart

Install `gardenia` (Python 3.11+, zero third-party runtime dependencies):

```sh
uv tool install gardenia
gardenia --version
```

### Step 1 — Once per workstation (`gardenia host`)

Preview and apply host CLIs, safe Linux broker setup (preserving any existing
`cgroup-v2` enforced broker), user-scoped plugins across detected clients, and optional
quota reserves:

```sh
gardenia host setup --dry-run
gardenia host setup --reserve-pct 5 --host-reserve-credit 20 --min-remaining-credit 5
gardenia host check
```

### Step 2 — Once per repository (`gardenia repo`)

Inside your repository's primary checkout, insert the delimited
`<!-- gardenia:begin --> ... <!-- gardenia:end -->` guardrail block into `AGENTS.md`
(preserving every surrounding project-specific line), link `CLAUDE.md -> AGENTS.md`,
and persist your profile to `gardenia.toml`:

```sh
gardenia repo init --gate "bash scripts/check" --cpu 4 --memory-gb 4 --save-config
gardenia repo check
```

### Step 3 — Across your managed repositories (`gardenia repos`)

Register existing primary checkouts once (new `gardenia repo init` runs register primary
checkouts automatically while skipping linked worktrees), verify every repository in one
command, and run isolated-worktree policy or dependency upgrade sweeps:

```sh
gardenia repos scan --root ~/git
gardenia repos list
gardenia repos check --strict
gardenia repos upgrade --dry-run
gardenia repos upgrade --package howhard --file-issues
```

### Unified diagnostic

```sh
gardenia check --json
```

## Documentation

- [Quickstart](docs/quickstart.md) — step-by-step workstation setup and repository onboarding.
- [Host vs. repository boundary](docs/host-vs-repo.md) — complete side-by-side matrix of host state, tracked repo policy, and local `.git/` state.
- [Full-stack architecture](docs/architecture.md) — end-to-end Coordinator vs. Leaf Worker runtime lifecycle.
- [CLI and schema reference](docs/reference.md) — subcommands, flags, exit codes, `gardenia.toml`, and `gardenia-check-v1` JSON schema.
- [Contributor workflow](docs/contributing.md) and [Packaging and release](docs/releasing.md).

Licensed under the [MIT License](LICENSE).
