# Full-stack architecture

For a plain-English introduction to the five failure modes of autonomous coding agents,
what each tool solves and owns, and how a ticket moves across the stack, start with the
[ecosystem overview](index.md). This page specifies the exact **Coordinator vs. Leaf
Worker** execution contract, CLI handoff files, resource claims, and post-run feedback
protocol enforced by a `gardenia`-managed `AGENTS.md`.

## Coordinator vs. Leaf Worker lifecycle

The runtime protocol separates **coordinating** an epic or ticket queue from
**implementing** a single authorized leaf ticket. Only the coordinator routes and spawns
workers; an assigned leaf worker executes its ticket end-to-end inside a dedicated
`/var/tmp` Git worktree without recursively invoking `agroute`, `howhard`, or subagent
delegation, and never calls `agc` recursively from inside an already admitted `agc` job.

```text
+-----------------------------------------------------------------------------+
| COORDINATOR (interactive session in primary checkout or epic control plane) |
|                                                                             |
|  1. Triage, dependency DAG & upfront owner rulings                          |
|     -> agrein: ticket-delivery + epic-queue + owner-escalation              |
|                                                                             |
|  2. Filter eligible routes within live subscription quotas & credit reserves|
|     -> agr usage                                                            |
|     -> agr candidates --host <codex|claude|agy> --output /tmp/TASK-c.json   |
|        (if price_request != null: look up official prices -> agr prices     |
|         resolve --file /tmp/TASK-p.json -> re-emit candidates once)         |
|                                                                             |
|  3. Choose optimal (agent, model, effort) from history & semantic memory    |
|     -> howhard context / retrieve / decide (with independent baseline,      |
|        or standalone pick) -> /tmp/TASK-d.json                              |
|     -> agr plan --host <host> --decision /tmp/TASK-d.json --json            |
|                                                                             |
|  4. Honor needs_review, then dispatch leaf worker:                          |
|     - target_agent == host_agent -> native subagent tool                    |
|     - target_agent != host_agent -> returned non-interactive CLI command    |
+--------------------------------------+--------------------------------------+
                                       |
                                       v
+-----------------------------------------------------------------------------+
| LEAF WORKER (assigned one ticket; NEVER delegates recursively)              |
|                                                                             |
|  1. Follow agrein: ticket-delivery (+ bug-investigation for defects)        |
|  2. Create isolated worktree & venv under /var/tmp/<repo>-<N>-*             |
|     (primary checkout stays strictly read-only)                             |
|  3. Implement change + focused hermetic tests + docs + CHANGELOG.md         |
|  4. Run coordinated validation with integer byte resource claims:           |
|     -> agc run --no-overflow --resource cpu=4 --resource memory=4294967296  |
|        [--resource tmpfs=<bytes> --resource tmpfs_inodes=262144]            |
|        -- bash scripts/check                                                |
|  5. Open PR ("Closes #N") and gate + merge atomically:                      |
|     -> agc land <pr> --no-overflow --resource cpu=4                         |
|        --resource memory=4294967296 [...] -- bash scripts/check             |
|     (or verify diff & merge via forge when documentation-only exempt)       |
|  6. Verify origin/main, issue closure, empty git cherry, clean /var/tmp     |
|  7. Return session handoff to coordinator                                   |
+--------------------------------------+--------------------------------------+
                                       |
                                       v
+-----------------------------------------------------------------------------+
| COORDINATOR POST-RUN ACCOUNTING                                             |
|                                                                             |
|  1. Record actual outcome & measured usage following plan.feedback block:   |
|     -> howhard observe <decision_id> <clean|loud|silent|interrupted> ...    |
|     -> howhard usage collect / attach ... (matched_receipt or raw_native)   |
|     (later escaped bugs -> howhard attribute-failure skill)                 |
|  2. Distill durable lessons into repository semantic memory:                |
|     -> howhard memory learn-context / learn (or add / edit / remove)        |
+-----------------------------------------------------------------------------+
```

## Protocol invariants across boundaries

1. **Offline price resolution (`agroute` → calling agent → `agroute`)** — `agr` never
   fetches model pricing over the network. When `agr candidates` discovers an unpriced
   model, it returns a `price_request` payload; the coordinator resolves official
   first-party pricing (or classifies an internal harness identifier as `"alias"` or
   `"unlisted"`), persists it locally via `agr prices resolve`, and re-emits candidates
   once so `howhard` compares fully priced inventories whenever prices exist.
2. **Stateless baseline independence (`howhard`)** — During `context` / `retrieve` /
   `decide`, the initial task capability forecast runs in a fresh, independent context so
   prior debate or sunk-cost bias in the coordinator thread does not skew difficulty
   estimates. Both `agroute` (`<git-common-dir>/agroute/`) and `howhard`
   (`<git-common-dir>/howhard/`) store repository-local state under `--git-common-dir`, so
   observations and memories are shared automatically across linked worktrees of the same
   clone without being tracked in Git.
3. **Receipt matching vs. raw native feedback (`agr plan` → `howhard observe`)** — `agr
   plan` emits a `feedback` block specifying whether the executed worker matches the
   selected decision receipt (`matched_receipt`, scored directly against `decision_id`) or
   ran on a known different native cell (`raw_native`), preventing misattributed training
   signal when host controls substitute or override a model.
4. **Integer resource claims and single landing barrier (`agcoord`)** — `agc` resource
   claims use exact integer bytes for `memory` and `tmpfs` (with `tmpfs` paired with
   `tmpfs_inodes`, rendered as `tmpfs_inodes=262144` by `gardenia`). `agc land <pr>` is
   the sole repository merge barrier for code, test, config, or packaging changes; a
   documentation-only pull request whose changed file list stays strictly within the
   repository's `AGENTS.md` exemption skips `agc` and merges directly through the forge
   after diff verification.

## Where `gardenia` sits

`gardenia` never sits inside the coordinator-to-worker runtime path above. It provisions,
synchronizes, and audits the environment around that loop:

- **Workstation (`gardenia host`)** — `gardenia host setup` and `gardenia host check`
  ensure `howhard` (`>= 4.0.0`), `agr` (`>= 0.6.0`), `agc` (`>= 0.10.0` on Linux), the
  Linux `agc` broker (preserving active `cgroup-v2` enforcement), user-scoped client
  plugins (`codex`, `claude`, `agy`), and `~/.config/agroute/config.toml` reserves are
  installed and version-aligned.
- **Repository (`gardenia repo`)** — `gardenia repo init` and `gardenia repo check`
  render and verify the delimited `<!-- gardenia:begin --> ... <!-- gardenia:end -->`
  contract inside `AGENTS.md` (encoding the exact routing mode, gate command, integer byte
  `agc` resource claims, and documentation-only exemption from `gardenia.toml` or
  `[tool.gardenia]`), maintain `CLAUDE.md -> AGENTS.md`, and inspect
  `<git-common-dir>/{howhard,agroute}/` read-only.
- **Workstation fleet (`gardenia repos` & `gardenia check`)** — `gardenia check` reports
  host and repository findings together with `"scope": "host"` or `"scope": "repo"`, while
  `gardenia repos` (`scan`, `list`, `check`, `upgrade`) audits all registered primary
  checkouts and validates policy or dependency upgrades inside disposable `/var/tmp`
  worktrees without touching any primary checkout. See [Host vs. repository
  boundary](host-vs-repo.md) and the [CLI and schema reference](reference.md) for every
  file path and flag.
