# Full-stack architecture

`gardenia` ties four focused tools into one coherent workflow without collapsing their
responsibilities into a monolith. Each layer owns one clear boundary:

- **`agrein`** defines *how* work is scoped, isolated, escalated, and verified (pure
  Markdown Agent Skills, zero runtime).
- **`agroute` (`agr`)** answers *which agent and models are eligible right now* given live
  subscription quotas, credit reserves, and first-party rate cards, and resolves a chosen
  route into either a native subagent directive or a cross-agent CLI command.
- **`howhard`** answers *which eligible `(agent, model, effort)` cell maximizes expected
  quality per unit cost*, records actual outcomes and token usage, attributes escaped
  defects, and maintains writable semantic memory.
- **`agcoord` (`agc`)** owns *machine resource admission, kernel cgroup-v2 limits, and
  atomic gate-plus-merge publication* on a shared Linux workstation.

## Coordinator vs. Leaf Worker lifecycle

The stack separates **coordinating** an epic or ticket queue from **executing** a single
authorized leaf ticket so delegation never loops recursively and every worker operates in
complete Git and resource isolation.

```text
+-----------------------------------------------------------------------------+
| COORDINATOR (interactive session in primary checkout or epic control plane) |
|                                                                             |
|  1. Triage & escalate unsettled product/policy questions                    |
|     -> agrein: epic-queue + owner-escalation                                |
|                                                                             |
|  2. Filter eligible routes within live subscription quotas & credit reserves|
|     -> agr usage                                                            |
|     -> agr candidates --host <codex|claude|agy> --output /tmp/TASK-c.json   |
|        (if price_request != null: look up official prices -> agr prices     |
|         resolve --file /tmp/TASK-p.json -> re-emit candidates once)         |
|                                                                             |
|  3. Choose optimal (agent, model, effort) from history & semantic memory    |
|     -> howhard context / retrieve / decide (with independent baseline)      |
|     -> agr plan --host <host> --decision /tmp/TASK-d.json --json            |
|                                                                             |
|  4. Dispatch leaf worker (native subagent tool OR cross-agent CLI plan)     |
+--------------------------------------+--------------------------------------+
                                       |
                                       v
+-----------------------------------------------------------------------------+
| LEAF WORKER (assigned one ticket; NEVER delegates recursively)              |
|                                                                             |
|  1. Follow agrein: ticket-delivery (+ bug-investigation for defects)        |
|  2. Create isolated worktree & venv under /var/tmp/<repo>-<N>-*             |
|     (primary checkout stays read-only)                                      |
|  3. Implement change + focused hermetic tests + docs + CHANGELOG.md         |
|  4. Run coordinated validation with integer byte memory claims:             |
|     -> agc run --no-overflow --resource cpu=4 --resource memory=4294967296  |
|        -- bash scripts/check                                                |
|  5. Open PR ("Closes #N") and gate + merge atomically:                      |
|     -> agc land <pr> --no-overflow --resource cpu=4                         |
|        --resource memory=4294967296 -- bash scripts/check                   |
|  6. Verify origin/main, CI, empty git cherry, clean /var/tmp artifacts      |
|  7. Return four-part session handoff to coordinator                         |
+--------------------------------------+--------------------------------------+
                                       |
                                       v
+-----------------------------------------------------------------------------+
| COORDINATOR POST-RUN ACCOUNTING                                             |
|                                                                             |
|  1. Record outcome & measured usage following plan.feedback block:          |
|     -> howhard observe <decision_id> <clean|loud|silent> ...                |
|     -> howhard usage attach ... (matched_receipt or raw_native)             |
|  2. Distill durable lessons into repository semantic memory:                |
|     -> howhard memory add / update / remove                                 |
+-----------------------------------------------------------------------------+
```

## Where `gardenia` sits

`gardenia` does not sit in the hot path of routing, execution, or merge gating. It is the
**setup, policy synchronization, and health-check layer** around the four tools:

- Before work begins on a workstation, `gardenia host setup` and `gardenia host check`
  verify that the Python CLIs, Linux `agc` broker, user-scoped plugins across Codex,
  Claude Code, and Antigravity, and host quota reserves are installed and version-aligned.
- Before work begins in a repository, `gardenia repo init` renders the exact managed
  guardrail block into `AGENTS.md` (with integer byte memory claims for `agc`, the
  configured routing layer, and the docs-only exemption) and keeps `CLAUDE.md` linked.
- At any point, `gardenia check` diagnoses drift across both layers without mutating
  either one.
