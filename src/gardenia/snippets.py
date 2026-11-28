"""Built-in fallback copies of each built-in plugin's root ``AGENTS.snippet.md``.

``gardenia repo init`` and ``gardenia repos upgrade`` prefer the snippet shipped by an installed
plugin. These verbatim copies are stamped only when no installed client cache (or local
plugin source) provides one, so an offline host still renders a deterministic block.
"""

from __future__ import annotations

from types import MappingProxyType
from typing import Mapping

_AGREIN = """\
Use the installed agrein skills: ticket-delivery before changing code, docs,
config, infrastructure, or live state; epic-queue when decomposing multi-ticket
work or coordinating subagent workers; bug-investigation before fixing defects or
failing checks; and owner-escalation for unsettled product/policy rulings, live
operation approvals, and decision handoffs. An assigned leaf worker implements
its ticket directly without recursively delegating it.
"""

_AGROUTE = """\
Before delegating an authorized task to a worker, invoke the installed `agroute` and `howhard` skills to inspect live
quota, select an eligible route, and honor `needs_review` and the native or cross-agent execution plan; report
unavailable routing or exhausted quota instead of bypassing them. Afterward, record the truthful outcome and measured
usage in `howhard` memory. Assigned leaf workers execute their task directly, without recursive delegation.
"""

_HOWHARD = """\
## howhard routing and attribution

- Before delegating an authorized task to a worker, use the installed `howhard` skill to
  select the worker's native model and reasoning effort from the host's supported
  controls. Respect explicit user model choices and required review.
- Evaluate any independent baseline forecast in a fresh, stateless context. It is part
  of routing, so never route it through `howhard` again.
- Start the worker with the selected controls. If routing is unavailable or the controls
  cannot be enforced, report the limitation instead of bypassing routing.
- After execution, record the truthful outcome and measured usage under a stable
  execution ID, leaving unmeasured usage unknown. Then update memory with
  `howhard memory learn`.
- During every bug-fix task or requested audit of completed work, invoke the installed
  `attribute-failure` skill to trace whether a defect escaped an earlier accepted agent
  task. Amend only supported attribution in `howhard`; leave uncertain attribution
  unscored.
"""

_AGCOORD = """\
When running checks or landing pull requests, follow the installed `agcoord` skill: pass a
short plain-English `--label` and integer byte resource claims with `agc run`, and keep
heavy checks portable for overflow unless they are host-bound. Use one `agc land` request
for gate and publication. Never run `agc` recursively inside an admitted job or merge a
pull request directly.
"""

BUILTIN_SNIPPETS: Mapping[str, str] = MappingProxyType(
    {
        "agrein": _AGREIN,
        "agroute": _AGROUTE,
        "howhard": _HOWHARD,
        "agcoord": _AGCOORD,
    }
)
