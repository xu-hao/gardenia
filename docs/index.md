# gardenia

```{image} assets/gardenia.svg
:alt: gardenia fractal mark
:width: 160px
:align: center
```

`gardenia` is the zero-dependency bootstrapper, repository initializer, verifier, and
canonical full-stack guide for a five-tool coding-agent ecosystem: **`agrein`**,
**`agroute`**, **`howhard`**, **`agcoord`**, and **`gardenia`**.

Together, these tools let you run a mixture of different coding agents across real repositories and multi-ticket
epics without trashing your working tree, blowing through one subscription's rate limit,
overpaying for simple changes, starving your Linux workstation of RAM, or leaking
home-directory paths into Git.

## Why five focused tools instead of a monolith

Running autonomous coding agents on real codebases hits five distinct failure modes:

1. **Workspace and workflow chaos** — Left unguided, agents edit your primary checkout
   while you are reading it, patch surface symptoms without a failing reproduction test,
   or quietly invent product
   policies and mutate live state instead of asking the owner. Addressed by **`agrein`**.
2. **Blind subscription and quota burnout** — Pinning every task to one default model
   drains a single client's 5-hour session window, 7-day weekly cap, or dollar credit
   reserve while your other installed clients sit idle with headroom — or breaks on
   newly discovered unpriced models and cross-agent CLI handoffs. Addressed by
   **`agroute` (`agr`)**.
3. **Guessing task difficulty** — Sending routine edits to a slow max-effort frontier
   model wastes quota, money, and wall-clock time; sending subtle state or concurrency
   work to a shallow tier produces code that passes cursory checks and escapes into
   `main` as a silent bug, with no memory of past failures in the same codebase.
   Addressed by **`howhard`**.
4. **Workstation thrashing and unguarded merges** — Three or four agent worktrees running
   test suites at once on one Linux workstation fight over CPU, RAM, and scratch space
   until jobs time out or get OOM-killed for reasons unrelated to the code, while pull
   requests tested against an older `main` merge over each other and break the trunk.
   Addressed by **`agcoord` (`agc`)**.
5. **Host-versus-repository setup drift** — Keeping Python CLIs, a Linux `cgroup-v2`
   broker, user-scoped plugins across three agent clients, per-repository `AGENTS.md`
   guardrails, local `.git/` stores, and a multi-repository fleet aligned by hand leads
   to version skew, accidentally downgraded brokers, or machine-local paths committed to
   Git. Addressed by **`gardenia`**.

Each failure mode lives at a different system boundary: contributor workflow habits,
multi-client subscription windows, per-repository Bayesian difficulty learning, OS kernel
resource scheduling with atomic Git merges, and out-of-band workstation/repository
provisioning. Splitting them into five small tools keeps every boundary explicit and lets
you adopt, upgrade, or debug any single layer without coupling a Linux `cgroup-v2` broker
to an LLM cost model or baking user-home paths into repository commits.

## Each tool in plain English

### `agrein`

- **Problem solved:** Stops agents from improvising destructive repository habits — editing
  the primary checkout, skipping issue tracking, fixing bugs without reproducing them
  first, looping in recursive subagent chains, or making product and live-state decisions
  without owner approval.
- **How it works:** Ships four pure-Markdown Agent Skills installed natively into Codex,
  Claude Code, and Antigravity:
  - `ticket-delivery` — claim or file `#N — one sentence describing the work` before
    editing, work exclusively in an isolated `/var/tmp` Git worktree branched from
    `origin/main`, capture complete test output once without truncating pipes, land via a
    guarded pull request (`Closes #N`), verify squash-safe equivalence with `git cherry`,
    clean up `/var/tmp` artifacts, and close with a concise handoff.
  - `epic-queue` — organize multi-ticket work with native GitHub sub-issues and an
    explicit dependency DAG, run parallel read-only probes upfront, batch owner rulings in
    the foreground, and dispatch leaf workers across the ready frontier without recursive
    re-delegation.
  - `bug-investigation` — reproduce defects before touching production code, prove whether
    a failure already exists on `origin/main`, fix root-cause producers rather than adding
    consumer fallbacks, and start every fix with a failing behavioral test.
  - `owner-escalation` — separate engineering choices from unsettled product/policy
    rulings and per-operation live approvals (deployments, tags/releases, PyPI uploads,
    host installations), presenting jargon-free before/after options.
- **Exact boundary:** Owns static workflow contracts (`SKILL.md` and reference guides). It
  has **zero** Python runtime, background daemon, or mutable state store, and it never
  chooses models, inspects quotas, or enforces OS resource limits.

### `agroute`

- **Problem solved:** Keeps delegated work inside your real subscription limits and dollar
  credit reserves across Codex (`codex`), Claude Code (`claude`), and Antigravity (`agy`),
  and translates a chosen route into the exact launch mechanism the current host needs.
- **How it works:** Discovers locally available models and reasoning efforts, attaches
  per-token rate cards (`howhard-rate-card-v1`), filters out routes that would breach
  session/weekly caps or credit reserves, and resolves `howhard` selections into execution
  plans:
  - `agr usage` — inspects or refreshes live subscription quota windows and calibrated
    credit ceilings.
  - `agr candidates --host <codex|claude|agy> --output /tmp/TASK-candidates.json --json` —
    emits currently eligible `<agent>/<model>` candidates with rate cards and available
    credit budgets, plus a `price_request` contract if a newly discovered model lacks a
    known price.
  - `agr prices resolve --file /tmp/TASK-prices.json --json` — records calling-agent
    first-party price lookups (or `"alias"` / `"unlisted"` classifications) locally
    offline.
  - `agr plan --host <codex|claude|agy> --decision /tmp/TASK-decision.json --json` —
    resolves a `howhard` decision into either a native subagent directive (`swarm` /
    `invoke_subagent`, `Agent`, or `spawn_agent` when `target_agent == host_agent`) or a
    non-interactive CLI command (`codex exec`, `claude -p`, or `agy -p` when
    `target_agent != host_agent`), alongside a `feedback` block (`matched_receipt` or
    `raw_native`).
- **Exact boundary:** Owns host quota and reserve settings in
  `${XDG_CONFIG_HOME:-~/.config}/agroute/config.toml`, optional tracked project overrides
  in `agroute.toml`, and per-clone route/usage/price state under `<git-common-dir>/agroute/`
  (`routes.sqlite3`, `prices.json`, `usage.json`). It never fetches prices over the
  network, never estimates task difficulty or stores semantic memory, and never spawns
  worker processes itself.

### `howhard`

- **Problem solved:** Decides how capable a model and how much reasoning effort a specific
  ticket actually needs — minimizing expected cost to an accepted result while capping the
  risk of silent defects among accepted work — and learns from real outcomes in that
  repository over time.
- **How it works:** Combines an independent LLM task reading with a per-repository
  Bayesian capability model and writable semantic memory:
  - `howhard context`, `howhard retrieve`, and `howhard decide` (using an independent
    stateless baseline forecast in a fresh context, or standalone `howhard pick`) — narrow
    the candidate inventory to a task-specific capability-fit band and forecast `clean`,
    caught (`loud`), and escaped (`silent`) outcomes, token usage, and wall-clock time.
  - `howhard observe` (plus `howhard usage collect` / `howhard usage attach` when host
    usage artifacts are available) — records actual `clean`, `loud`, `silent`, or
    `interrupted` outcomes and measured usage against the decision receipt or raw native
    identity.
  - `attribute-failure` (native skill) — traces an escaped silent bug discovered later
    back to the execution that introduced it so future routing accounts for the miss.
  - `howhard memory` (`learn-context`, `learn`, `list`, `read`, `search`, `add`, `edit`,
    `remove`) — distills durable repository lessons whose predictive support updates
    alongside the threshold reader.
- **Exact boundary:** Owns optional tracked project settings in `howhard.toml` and the
  per-clone belief, payload, and semantic-memory store under `<git-common-dir>/howhard/`
  (`store.json`, `payloads.sqlite3`, `memory.sqlite3`, shared across linked Git worktrees).
  It never inspects multi-client subscription quotas, never launches subagents, and never
  manages OS CPU/memory limits or Git merges.

### `agcoord`

- **Problem solved:** Prevents concurrent agent test runs from starving a shared Linux
  machine of CPU, RAM, or `tmpfs`, and prevents pull requests from merging on stale or
  failing test evidence.
- **How it works:** Runs one detached unprivileged coordinator broker per OS user that
  queues jobs across every repository and worktree on the workstation:
  - `agc run` (and `agc full` for clean exact-head receipts) — admits validation commands
    only when declared resources fit machine capacity, using integer byte claims for
    memory and scratch space (for example `--resource cpu=4 --resource memory=4294967296`,
    plus paired `--resource tmpfs=<bytes> --resource tmpfs_inodes=262144` when scratch RAM
    disk is configured) and enforcing real kernel limits via delegated `cgroup-v2` slices
    on managed Linux hosts.
  - `agc land <pr>` — holds the repository's landing barrier and declared resources across
    one atomic gate-and-publish step: syncing the current target branch into the pull
    request (or refusing stale heads), running the full validation gate once on the exact
    resulting tree, and merging only when the gate exits `0` (publishing nothing on
    `gate-failed`, `stale-main`, or `head-changed`).
- **Exact boundary:** Owns workstation job admission, `cgroup-v2` isolation, durable run
  logs under `${XDG_STATE_HOME:-~/.local/state}/agcoord`, and atomic pull-request
  publication. It never picks models, reads prompt semantics, or edits application source
  files.

### `gardenia`

- **Problem solved:** Eliminates manual installation skew and host-versus-repository
  configuration drift across all four tools, three coding-agent clients, single checkouts,
  and multi-repository workstation fleets.
- **How it works:** Provides one Python 3.11+ CLI with zero third-party runtime
  dependencies split strictly by state scope:
  - `gardenia host` (`setup`, `check`) — installs and verifies `howhard`, `agr`, and
    `agc` on `PATH`, configures or preserves the Linux `agc` broker (never downgrading an
    active `cgroup-v2` enforced broker to user mode), installs user-scoped plugins across
    Codex, Claude Code, and Antigravity, and writes host quota reserves in
    `~/.config/agroute/config.toml`.
  - `gardenia repo` (`init`, `render`, `check`) — updates **only** the delimited
    `<!-- gardenia:begin --> ... <!-- gardenia:end -->` guardrail block inside
    `AGENTS.md` (preserving all project-specific rules outside those markers), manages
    `CLAUDE.md -> AGENTS.md` and `gardenia.toml`, and checks per-clone `.git/` stores
    read-only.
  - `gardenia repos` (`scan`, `list`, `add`, `remove`, `prune`, `check`, `upgrade`) —
    maintains the host repository registry (`${XDG_CONFIG_HOME:-~/.config}/gardenia/repos.toml`),
    audits every registered repository read-only, and runs isolated `/var/tmp` worktree
    policy and dependency upgrade sweeps without ever touching a primary checkout.
  - `gardenia check` — runs `host check` and `repo check` together, tagging every finding
    with `"scope": "host"` or `"scope": "repo"` and a concrete remediation command.
- **Exact boundary:** Sits completely outside the live ticket-execution hot path. It
  enforces the three-zone separation between per-host workstation state, tracked
  repository policy, and untracked local `<git-common-dir>/{howhard,agroute}/` stores
  (inspected read-only) — never routing tasks, replacing `agc` during gates, or mutating
  primary checkouts during fleet upgrades.

## How they work together on one ticket

When an authorized ticket runs on a `gardenia`-managed workstation and repository, the
tools hand off cleanly in six steps:

1. **Triage and scope (`agrein`)** — In the coordinating session, the agent verifies or
   files a `#N — ...` GitHub issue (`ticket-delivery`), sequences dependencies if the task
   belongs to a multi-ticket epic (`epic-queue`), and asks the owner upfront if any
   product rule or live-operation approval is unsettled (`owner-escalation`).
2. **Filter live quota and pricing (`agroute`)** — Before spawning a worker, the
   coordinator checks current subscription windows (`agr usage`) and emits eligible
   provider-qualified routes (`agr candidates --host <codex|claude|agy> --output /tmp/TASK-candidates.json --json`),
   resolving any unpriced newly discovered models once offline via `agr prices resolve` if
   `price_request` is non-null.
3. **Choose `(agent, model, effort)` and resolve the launch plan (`howhard` → `agr plan`)** —
   `howhard` (`context` / `retrieve` / `decide` with an independent stateless baseline
   forecast, or `pick`) weighs task difficulty and repository semantic memory against
   expected cost to pick one route, and `agr plan --host <codex|claude|agy> --decision /tmp/TASK-decision.json --json`
   converts that decision into either a native subagent call (`target_agent == host_agent`)
   or a non-interactive cross-agent CLI command (`target_agent != host_agent`).
4. **Implement directly in an isolated `/var/tmp` worktree (`agrein`)** — The dispatched
   leaf worker never delegates recursively: it creates an issue-specific Git worktree and
   virtual environment under `/var/tmp` branched from `origin/main` (keeping the primary
   checkout read-only), starts bug fixes with a failing behavioral test
   (`bug-investigation`), and implements the code, hermetic tests, and docs.
5. **Run `cgroup-v2`-bounded checks and merge atomically (`agcoord`)** — On Linux, the
   leaf worker runs validation through `agc run` with integer byte `memory` (and optional
   `tmpfs` + `tmpfs_inodes`) claims so parallel workers never thrash the host, opens a
   pull request (`Closes #N`), and lands it in one atomic gate-and-merge operation via
   `agc land <pr>` (or merges directly through the forge when every changed file qualifies
   as documentation-only), then removes its `/var/tmp` worktree and branch.
6. **Record outcome, usage, and semantic memory (`howhard`)** — After the worker returns
   its handoff, the coordinator records the real outcome and measured token/time usage in
   `howhard` (`howhard observe` and `howhard usage attach`, following the plan's
   `matched_receipt` or `raw_native` feedback contract) and updates repository semantic
   memory (`howhard memory learn-context` / `learn`) so the next ticket benefits from what
   just happened.

**Where `gardenia` fits around this loop:** `gardenia` never adds latency or moving parts
inside steps 1–6. Instead, `gardenia host setup` prepares the workstation once,
`gardenia repo init` writes the exact `AGENTS.md` contract that tells agents how to run
steps 1–6, `gardenia check` verifies at any time that neither host state nor repository
policy has drifted, and `gardenia repos` keeps every managed checkout on your machine
healthy over time. See [Full-stack architecture](architecture.md) for the exact
Coordinator vs. Leaf Worker command sequence and [Quickstart](quickstart.md) to set up
your machine and first repository.

## Getting started

- [Quickstart](quickstart.md) — Step 1 once per workstation (`gardenia host setup` &
  `gardenia host check`) and Step 2 once per repository (`gardenia repo init` &
  `gardenia repo check`).
- [Host vs. repository boundary](host-vs-repo.md) — complete side-by-side matrix of
  what belongs on the workstation, what is tracked in Git, and what stays local under
  `.git/`.

```{toctree}
:hidden:
:caption: Getting started

quickstart
host-vs-repo
```

## Architecture & Reference

- [Full-stack architecture](architecture.md) — end-to-end Coordinator vs. Leaf Worker
  runtime lifecycle across `agrein`, `agroute`, `howhard`, and `agcoord`.
- [CLI and schema reference](reference.md) — exact subcommands, flags, exit codes,
  `gardenia.toml` / `[tool.gardenia]` schema, and the `gardenia-check-v1` JSON report.

```{toctree}
:hidden:
:caption: Architecture & Reference

architecture
reference
```

## Project

- [Contributor workflow](contributing.md) — issues, isolated worktrees, coordinated
  checks, and atomic landing.
- [Packaging and release](releasing.md) — versioning, wheel verification, and release
  guardrails.
- [Changelog](changelog.md) — release history.
- [Image provenance](assets/README.md) — mathematical construction and checksum of
  `gardenia.svg`.
- [Session handoff](session_handoff_format.md) — four-part reporting format for changed
  work.

```{toctree}
:hidden:
:caption: Project

contributing
releasing
changelog
assets/README
session_handoff_format
```
