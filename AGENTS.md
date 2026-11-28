# gardenia contributor instructions

Keep this file limited to repository-wide guardrails. Follow the canonical contracts and
procedures listed in [docs/index.md](docs/index.md), the
[contributor workflow](docs/contributing.md), the
[CLI and schema reference](docs/reference.md), and the
[release guide](docs/releasing.md). End changed work with the four-part
[session handoff](docs/session_handoff_format.md).

<!-- gardenia:begin -->
- File an issue (`#N — one sentence describing the work`) for each code, test, docs, or packaging
  change; work in an issue-named `/var/tmp` worktree from `origin/main`, keep the primary checkout
  read-only, and keep check logs complete (never truncated).
- Verify behavior at public boundaries with focused tests (bug fixes start with a failing one) and
  add canonical docs plus a `CHANGELOG.md` entry for user-facing changes. Tests stop and remove all
  they create and never touch `~/.codex`, `~/.claude`, `~/.gemini`, `~/.config`, or `~/.local`.
- Docs-only changes (every path `README.md`, `CHANGELOG.md`, `AGENTS.md`, Markdown in `docs/`, or
  a `docs/assets/` `.png`/`.svg` with provenance in `docs/assets/README.md`) skip tests and the
  gate; verify the PR file list and diff, then merge via the forge.
- Gate through `agc run` / `agc land <pr>` where `agc` is available, passing
  `--no-overflow --resource cpu=4 --resource memory=4294967296 -- bash scripts/check`; elsewhere,
  run `bash scripts/check` and merge via the forge after it passes.
- Never put private project names, machine-local paths, or other repositories' issue links in
  tracked files, commits, or pull requests.
- PyPI uploads and live host or production changes need explicit owner approval per operation.
<!-- gardenia:end -->

<!-- agrein:begin -->
Use the installed agrein skills: ticket-delivery before changing code, docs,
config, infrastructure, or live state; epic-queue when decomposing multi-ticket
work or coordinating subagent workers; bug-investigation before fixing defects or
failing checks; and owner-escalation for unsettled product/policy rulings, live
operation approvals, and decision handoffs. An assigned leaf worker implements
its ticket directly without recursively delegating it.
<!-- agrein:end -->

<!-- agroute:begin -->
Before delegating an authorized task to a worker, invoke the installed `agroute` and `howhard` skills to inspect live
quota, select an eligible route, and honor `needs_review` and the native or cross-agent execution plan; report
unavailable routing or exhausted quota instead of bypassing them. Afterward, record the truthful outcome and measured
usage in `howhard` memory. Assigned leaf workers execute their task directly, without recursive delegation.
<!-- agroute:end -->

<!-- agcoord:begin -->
When running checks or landing pull requests, follow the installed `agcoord` skill: pass a
short plain-English `--label` and integer byte resource claims with `agc run`, and keep
heavy checks portable for overflow unless they are host-bound. Use one `agc land` request
for gate and publication. Never run `agc` recursively inside an admitted job or merge a
pull request directly.
<!-- agcoord:end -->
