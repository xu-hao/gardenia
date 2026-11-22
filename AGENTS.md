# walledgarden contributor instructions

- Use the installed `agrein` skills: `ticket-delivery` before changing code, docs,
  config, infrastructure, or live state; `epic-queue` when decomposing multi-ticket
  work or coordinating subagent workers; `bug-investigation` before fixing defects or
  failing checks; and `owner-escalation` for unsettled product/policy rulings, live
  operation approvals, and decision handoffs.
- Coordinate ticket work by delegating each authorized ticket to a worker selected with
  the installed `agroute` and `howhard` skills; an assigned leaf worker implements its
  ticket directly without recursively delegating it.
- Track every code, test, documentation, or packaging change with a descriptive GitHub
  issue (`#N — one sentence describing the work`). Develop and validate only in an
  issue-specific Git worktree branched from `origin/main`; keep the primary checkout
  read-only during development.
- Coordinate checks and PR landing through `agcoord` (`agc run` and `agc land` with
  `bash scripts/check` as the gate and integer byte memory claims). Never submit
  coordinated work recursively inside an admitted job.
- Never upload to PyPI or modify live host installations unless the owner explicitly
  authorizes that exact operation.
- Tests must own and isolate all temporary directories, fake binaries, and subprocesses
  they create, and must never mutate real `~/.codex`, `~/.claude`, `~/.gemini`,
  `~/.config`, or `~/.local` host state.
