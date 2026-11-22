"""Per-repository policy rendering, initialization, and diagnostics (strictly isolated from host state)."""

from __future__ import annotations

from dataclasses import asdict, dataclass
import json
import os
from pathlib import Path
import shutil
import tomllib
from typing import Any, Mapping

from gardenia import __version__
from gardenia.models import (
    CHECK_FORMAT,
    DEFAULT_TIMEOUT_SECONDS,
    MANAGED_BEGIN,
    MANAGED_END,
    Finding,
    GardeniaError,
    PlannedAction,
    compute_ok,
    is_finite_number,
    run_cmd,
)

GIB = 1024 * 1024 * 1024

ALLOWED_ROUTING = ("agroute", "howhard", "none")
ALLOWED_AGCOORD = ("auto", "required", "none")

CONFIG_KEYS = frozenset(
    {
        "gate",
        "cpu",
        "memory_gb",
        "tmpfs_gb",
        "no_overflow",
        "routing",
        "agcoord",
        "docs_exemption",
        "claude_symlink",
    }
)


@dataclass(frozen=True)
class RepoProfile:
    """Tracked repository profile controlling managed AGENTS.md instructions."""

    gate: str = "bash scripts/check"
    cpu: int = 4
    memory_gb: float = 4.0
    tmpfs_gb: float | None = None
    no_overflow: bool = True
    routing: str = "agroute"
    agcoord: str = "auto"
    docs_exemption: bool = True
    claude_symlink: bool = True

    def validate(self) -> None:
        if not isinstance(self.gate, str) or not self.gate.strip():
            raise GardeniaError("Repo profile 'gate' must be a non-empty string.", exit_code=2)
        if isinstance(self.cpu, bool) or not isinstance(self.cpu, int) or self.cpu < 1:
            raise GardeniaError("Repo profile 'cpu' must be an integer >= 1.", exit_code=2)
        if not is_finite_number(self.memory_gb) or float(self.memory_gb) <= 0.0:
            raise GardeniaError("Repo profile 'memory_gb' must be a positive finite number.", exit_code=2)
        if self.tmpfs_gb is not None and (not is_finite_number(self.tmpfs_gb) or float(self.tmpfs_gb) <= 0.0):
            raise GardeniaError("Repo profile 'tmpfs_gb' must be a positive finite number when set.", exit_code=2)
        if not isinstance(self.no_overflow, bool):
            raise GardeniaError("Repo profile 'no_overflow' must be a boolean.", exit_code=2)
        if self.routing not in ALLOWED_ROUTING:
            raise GardeniaError(
                f"Repo profile 'routing' must be one of: {', '.join(ALLOWED_ROUTING)}.",
                exit_code=2,
            )
        if self.agcoord not in ALLOWED_AGCOORD:
            raise GardeniaError(
                f"Repo profile 'agcoord' must be one of: {', '.join(ALLOWED_AGCOORD)}.",
                exit_code=2,
            )
        if not isinstance(self.docs_exemption, bool):
            raise GardeniaError("Repo profile 'docs_exemption' must be a boolean.", exit_code=2)
        if not isinstance(self.claude_symlink, bool):
            raise GardeniaError("Repo profile 'claude_symlink' must be a boolean.", exit_code=2)

    def memory_bytes(self) -> int:
        return int(round(float(self.memory_gb) * GIB))

    def tmpfs_bytes(self) -> int | None:
        if self.tmpfs_gb is None:
            return None
        return int(round(float(self.tmpfs_gb) * GIB))

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class GitRepoInfo:
    """Resolved Git repository root and common state directory."""

    root: Path
    git_dir: Path
    git_common_dir: Path
    is_linked_worktree: bool


def _effective_env(env: Mapping[str, str] | None) -> dict[str, str]:
    return dict(os.environ if env is None else env)


def resolve_git_repo(
    repo_path: str | os.PathLike[str] | None = None,
    *,
    env: Mapping[str, str] | None = None,
) -> GitRepoInfo:
    """Resolve Git top-level, git-dir, and git-common-dir; refuse non-Git directories."""

    target = Path(repo_path).resolve() if repo_path is not None else Path.cwd().resolve()
    if not target.exists():
        raise GardeniaError(f"Repository path does not exist: {target}", exit_code=2)
    if not target.is_dir():
        raise GardeniaError(f"Repository path is not a directory: {target}", exit_code=2)

    res = run_cmd(
        ["git", "rev-parse", "--show-toplevel", "--git-dir", "--git-common-dir"],
        cwd=target,
        timeout=DEFAULT_TIMEOUT_SECONDS,
        env=_effective_env(env),
    )
    if not res.ok:
        raise GardeniaError(
            f"Not inside a Git repository: {target}",
            exit_code=2,
        )

    lines = [line.strip() for line in res.stdout.splitlines() if line.strip()]
    if len(lines) < 3:
        raise GardeniaError(
            f"Unexpected output from git rev-parse in {target}: {res.stdout.strip()}",
            exit_code=2,
        )
    root = Path(lines[0]).resolve()
    raw_git_dir = Path(lines[1])
    raw_common_dir = Path(lines[2])
    git_dir = raw_git_dir.resolve() if raw_git_dir.is_absolute() else (target / raw_git_dir).resolve()
    git_common_dir = raw_common_dir.resolve() if raw_common_dir.is_absolute() else (target / raw_common_dir).resolve()
    return GitRepoInfo(
        root=root,
        git_dir=git_dir,
        git_common_dir=git_common_dir,
        is_linked_worktree=(git_dir != git_common_dir),
    )


def try_resolve_git_repo(
    repo_path: str | os.PathLike[str] | None = None,
    *,
    env: Mapping[str, str] | None = None,
) -> GitRepoInfo | None:
    """Resolve Git repository info or return None when outside a Git repo."""

    try:
        return resolve_git_repo(repo_path, env=env)
    except GardeniaError:
        return None


def _parse_profile_table(table: dict[str, Any], source_label: str) -> RepoProfile:
    unknown = sorted(set(table.keys()) - CONFIG_KEYS)
    if unknown:
        raise GardeniaError(
            f"Unknown key(s) in {source_label}: {', '.join(unknown)}. "
            f"Allowed keys: {', '.join(sorted(CONFIG_KEYS))}.",
            exit_code=2,
        )
    defaults = RepoProfile()
    kwargs: dict[str, Any] = defaults.to_dict()
    for key, val in table.items():
        if key in ("memory_gb", "tmpfs_gb") and not isinstance(val, bool) and isinstance(val, (int, float)):
            kwargs[key] = float(val)
        else:
            kwargs[key] = val
    profile = RepoProfile(**kwargs)
    profile.validate()
    return profile


def load_repo_config(repo_root: Path) -> dict[str, Any]:
    """Load tracked gardenia profile from ``gardenia.toml`` or ``pyproject.toml`` ``[tool.gardenia]``."""

    gardenia_toml = repo_root / "gardenia.toml"
    pyproject_toml = repo_root / "pyproject.toml"

    has_gardenia_toml = gardenia_toml.is_file()
    pyproject_table: dict[str, Any] | None = None
    pyproject_parse_error: str | None = None
    has_pyproject_section = False

    if pyproject_toml.is_file():
        try:
            py_data = tomllib.loads(pyproject_toml.read_text(encoding="utf-8"))
        except (OSError, tomllib.TOMLDecodeError) as exc:
            pyproject_parse_error = str(exc)
        else:
            tool_section = py_data.get("tool")
            if isinstance(tool_section, dict) and "gardenia" in tool_section:
                has_pyproject_section = True
                raw_section = tool_section["gardenia"]
                if isinstance(raw_section, dict):
                    pyproject_table = raw_section
                else:
                    pyproject_parse_error = "[tool.gardenia] must be a TOML table."

    if has_gardenia_toml:
        try:
            raw_data = tomllib.loads(gardenia_toml.read_text(encoding="utf-8"))
        except (OSError, tomllib.TOMLDecodeError) as exc:
            return {
                "source": "gardenia.toml",
                "path": str(gardenia_toml),
                "valid": False,
                "profile": RepoProfile(),
                "values": RepoProfile().to_dict(),
                "error": str(exc),
                "duplicate_sources": has_pyproject_section,
            }
        try:
            profile = _parse_profile_table(raw_data, str(gardenia_toml))
        except GardeniaError as exc:
            return {
                "source": "gardenia.toml",
                "path": str(gardenia_toml),
                "valid": False,
                "profile": RepoProfile(),
                "values": RepoProfile().to_dict(),
                "error": exc.message,
                "duplicate_sources": has_pyproject_section,
            }
        return {
            "source": "gardenia.toml",
            "path": str(gardenia_toml),
            "valid": True,
            "profile": profile,
            "values": profile.to_dict(),
            "error": None,
            "duplicate_sources": has_pyproject_section,
        }

    if has_pyproject_section or (pyproject_toml.is_file() and pyproject_parse_error is not None and not has_gardenia_toml):
        if pyproject_parse_error is not None:
            return {
                "source": "pyproject.toml",
                "path": str(pyproject_toml),
                "valid": False,
                "profile": RepoProfile(),
                "values": RepoProfile().to_dict(),
                "error": pyproject_parse_error,
                "duplicate_sources": False,
            }
        assert pyproject_table is not None
        try:
            profile = _parse_profile_table(pyproject_table, f"{pyproject_toml} [tool.gardenia]")
        except GardeniaError as exc:
            return {
                "source": "pyproject.toml",
                "path": str(pyproject_toml),
                "valid": False,
                "profile": RepoProfile(),
                "values": RepoProfile().to_dict(),
                "error": exc.message,
                "duplicate_sources": False,
            }
        return {
            "source": "pyproject.toml",
            "path": str(pyproject_toml),
            "valid": True,
            "profile": profile,
            "values": profile.to_dict(),
            "error": None,
            "duplicate_sources": False,
        }

    default_profile = RepoProfile()
    return {
        "source": "defaults",
        "path": None,
        "valid": True,
        "profile": default_profile,
        "values": default_profile.to_dict(),
        "error": None,
        "duplicate_sources": False,
    }


def merge_profile_overrides(
    base: RepoProfile,
    *,
    gate: str | None = None,
    cpu: int | None = None,
    memory_gb: float | None = None,
    tmpfs_gb: float | None = None,
    no_overflow: bool | None = None,
    routing: str | None = None,
    agcoord: str | None = None,
    docs_exemption: bool | None = None,
    claude_symlink: bool | None = None,
) -> RepoProfile:
    """Overlay explicit CLI flags onto a loaded RepoProfile and validate."""

    merged = RepoProfile(
        gate=base.gate if gate is None else gate,
        cpu=base.cpu if cpu is None else cpu,
        memory_gb=base.memory_gb if memory_gb is None else float(memory_gb),
        tmpfs_gb=base.tmpfs_gb if tmpfs_gb is None else float(tmpfs_gb),
        no_overflow=base.no_overflow if no_overflow is None else no_overflow,
        routing=base.routing if routing is None else routing,
        agcoord=base.agcoord if agcoord is None else agcoord,
        docs_exemption=base.docs_exemption if docs_exemption is None else docs_exemption,
        claude_symlink=base.claude_symlink if claude_symlink is None else claude_symlink,
    )
    merged.validate()
    return merged


def render_gardenia_toml(profile: RepoProfile) -> str:
    """Serialize a RepoProfile to clean, deterministic ``gardenia.toml``."""

    profile.validate()
    lines = [
        "# Tracked repository profile managed by gardenia.",
        f"gate = {json.dumps(profile.gate)}",
        f"cpu = {profile.cpu}",
        f"memory_gb = {float(profile.memory_gb):g}",
    ]
    if profile.tmpfs_gb is not None:
        lines.append(f"tmpfs_gb = {float(profile.tmpfs_gb):g}")
    lines.extend(
        [
            f"no_overflow = {'true' if profile.no_overflow else 'false'}",
            f"routing = {json.dumps(profile.routing)}",
            f"agcoord = {json.dumps(profile.agcoord)}",
            f"docs_exemption = {'true' if profile.docs_exemption else 'false'}",
            f"claude_symlink = {'true' if profile.claude_symlink else 'false'}",
            "",
        ]
    )
    return "\n".join(lines)


def _format_agc_flags(profile: RepoProfile) -> str:
    parts: list[str] = []
    if profile.no_overflow:
        parts.append("--no-overflow")
    parts.append(f"--resource cpu={profile.cpu}")
    parts.append(f"--resource memory={profile.memory_bytes()}")
    tmpfs = profile.tmpfs_bytes()
    if tmpfs is not None:
        parts.append(f"--resource tmpfs={tmpfs}")
    return " ".join(parts)


def render_managed_block(profile: RepoProfile) -> str:
    """Render the canonical delimited ``<!-- gardenia:begin --> ... <!-- gardenia:end -->`` block."""

    profile.validate()
    agc_flags = _format_agc_flags(profile)
    bullets: list[str] = [
        (
            "- Use the installed `agrein` skills: `ticket-delivery` before changing code, docs,\n"
            "  config, infrastructure, or live state; `epic-queue` when decomposing multi-ticket\n"
            "  work or coordinating subagent workers; `bug-investigation` before fixing defects or\n"
            "  failing checks; and `owner-escalation` for unsettled product/policy rulings, live\n"
            "  operation approvals, and decision handoffs."
        )
    ]

    if profile.routing == "agroute":
        bullets.append(
            "- Coordinate ticket work by delegating each authorized ticket to a worker selected with\n"
            "  the installed `agroute` and `howhard` skills; an assigned leaf worker implements its\n"
            "  ticket directly without recursively delegating it (report a capability gap first if\n"
            "  `agroute` or worker delegation is unavailable on the host):\n"
            "  1. Inspect or refresh live agent quota state with `agr usage`.\n"
            "  2. Emit eligible provider-qualified candidates for this host with\n"
            "     `agr candidates --host <codex|claude|agy> --output /tmp/TASK-candidates.json --json`.\n"
            "     If `price_request` is non-null, look up official first-party provider pricing (or\n"
            "     classify internal harness models as `\"alias\"` or `\"unlisted\"`), run\n"
            "     `agr prices resolve --file /tmp/TASK-prices.json --json`, and re-emit candidates once.\n"
            "  3. Select a route with `howhard` (`context`/`retrieve`/`decide` with an independent\n"
            "     stateless baseline forecast, or `pick`), then resolve the execution plan with\n"
            "     `agr plan --host <codex|claude|agy> --decision /tmp/TASK-decision.json --json`.\n"
            "  4. Honor `needs_review`. Execute native plans (`target_agent == host_agent`) with the\n"
            "     host's native subagent tool and cross-agent plans (`target_agent != host_agent`) with\n"
            "     the returned non-interactive CLI command.\n"
            "  5. Record the actual outcome and measured usage in `howhard` following the plan's\n"
            "     `feedback` block (`matched_receipt` or `raw_native`), then update `howhard` memory."
        )
    elif profile.routing == "howhard":
        bullets.append(
            "- Coordinate ticket work by delegating each authorized ticket to a native worker\n"
            "  selected with the installed `howhard` skill (`context`/`retrieve`/`decide` with an\n"
            "  independent stateless baseline forecast, or `pick`), then record the actual outcome\n"
            "  and measured usage in `howhard` (`observe`, `usage attach`) and update `howhard`\n"
            "  memory; an assigned leaf worker implements its ticket directly without recursively\n"
            "  delegating it."
        )
    else:
        bullets.append(
            "- An assigned leaf worker implements its ticket directly without recursively delegating it."
        )

    bullets.append(
        "- Track every code, test, documentation, or packaging change with a descriptive issue\n"
        "  (`#N — one sentence describing the work`). Do not file an issue solely for\n"
        "  machine-local installation, deployment, configuration, service lifecycle, or other\n"
        "  operator state unless it also requires a repository change or the user requests one."
    )
    bullets.append(
        "- Develop and validate only in an issue-specific Git worktree branched from\n"
        "  `origin/main`. Preserve unrelated work and keep the primary checkout read-only during\n"
        "  development."
    )
    bullets.append(
        "- Verify behavior at public boundaries. Start bug fixes with a failing behavioral test;\n"
        "  accompany runtime changes with focused tests, affected canonical documentation, and a\n"
        "  `CHANGELOG.md` entry when the change is user-facing."
    )

    if profile.docs_exemption:
        bullets.append(
            "- A documentation-only change needs no tests or AGCoord gate. It qualifies only when\n"
            "  every changed path is `README.md`, `CHANGELOG.md`, `AGENTS.md`, Markdown below\n"
            "  `docs/`, or a `.png` or `.svg` image below `docs/assets/` whose provenance is\n"
            "  recorded in `docs/assets/README.md`. Verify the exact pull-request file list and\n"
            "  diff, then merge it through the forge; the issue, worktree, branch, pull-request,\n"
            "  and no-direct-target-push rules still apply."
        )

    if profile.agcoord == "required":
        bullets.append(
            "- Route checks and pull-request landing through the local `agcoord` broker with\n"
            "  integer byte memory claims:\n"
            f"  - Check: `agc run {agc_flags} -- {profile.gate}`\n"
            f"  - Land: `agc land <pr> {agc_flags} -- {profile.gate}`\n"
            "  Never submit coordinated work recursively inside an admitted job, and never bypass or\n"
            "  separate the gate verdict from publication."
        )
    elif profile.agcoord == "auto":
        bullets.append(
            "- On Linux hosts with `agcoord` (`agc`), route checks and pull-request landing through\n"
            "  the local coordinator with integer byte memory claims:\n"
            f"  - Check: `agc run {agc_flags} -- {profile.gate}`\n"
            f"  - Land: `agc land <pr> {agc_flags} -- {profile.gate}`\n"
            "  Never submit coordinated work recursively inside an admitted job, and never bypass or\n"
            "  separate the gate verdict from publication. On hosts without `agc`, run\n"
            f"  `{profile.gate}` directly and merge through the forge after checks pass."
        )
    else:
        bullets.append(
            f"- Run `{profile.gate}` to validate every change before opening or merging a pull\n"
            "  request, and never push directly to `main`."
        )

    bullets.append(
        "- Tests must own and stop every process, repository, and temporary directory they\n"
        "  create, and must never inspect or mutate real host state (`~/.codex`, `~/.claude`,\n"
        "  `~/.gemini`, `~/.config`, or `~/.local`)."
    )
    bullets.append(
        "- Never upload a distribution to PyPI or modify live host/production state unless the\n"
        "  owner explicitly authorizes that exact operation. Permission to implement, test,\n"
        "  commit, push, merge, tag, or create a GitHub release does not authorize a PyPI upload."
    )

    body = "\n".join(bullets)
    return f"{MANAGED_BEGIN}\n{body}\n{MANAGED_END}"


def extract_managed_block(text: str) -> tuple[str | None, bool]:
    """Extract the managed block from ``AGENTS.md``.

    Returns ``(block_text_or_none, malformed)``.
    """

    begin_count = text.count(MANAGED_BEGIN)
    end_count = text.count(MANAGED_END)
    if begin_count == 0 and end_count == 0:
        return None, False
    if begin_count != 1 or end_count != 1:
        return None, True
    begin_idx = text.index(MANAGED_BEGIN)
    end_idx = text.index(MANAGED_END)
    if end_idx < begin_idx:
        return None, True
    return text[begin_idx : end_idx + len(MANAGED_END)], False


def apply_managed_block(existing: str | None, block: str) -> str:
    """Insert or replace ONLY the managed block while preserving all surrounding lines."""

    if existing is None:
        return f"# Repository agent instructions\n\n{block}\n"

    extracted, malformed = extract_managed_block(existing)
    if malformed:
        raise GardeniaError(
            f"Malformed gardenia markers in AGENTS.md: ensure exactly one '{MANAGED_BEGIN}' "
            f"precedes one '{MANAGED_END}'.",
            exit_code=2,
        )
    if extracted is not None:
        begin_idx = existing.index(MANAGED_BEGIN)
        end_idx = existing.index(MANAGED_END) + len(MANAGED_END)
        return existing[:begin_idx] + block + existing[end_idx:]

    if not existing:
        return f"# Repository agent instructions\n\n{block}\n"
    sep = "\n\n" if not existing.endswith("\n") else ("\n" if not existing.endswith("\n\n") else "")
    return f"{existing}{sep}{block}\n"


def _inspect_optional_toml(path: Path, component: str, findings: list[Finding]) -> dict[str, Any]:
    if not path.is_file():
        return {"path": str(path), "exists": False, "valid": None, "error": None}
    try:
        tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        findings.append(
            Finding(
                scope="repo",
                severity="error",
                component=component,
                message=f"Tracked repository config {path.name} has invalid TOML: {exc}",
                remediation=f"Fix TOML syntax in {path}",
            )
        )
        return {"path": str(path), "exists": True, "valid": False, "error": str(exc)}
    return {"path": str(path), "exists": True, "valid": True, "error": None}


def _inspect_claude_md(repo_root: Path, profile: RepoProfile, findings: list[Finding]) -> dict[str, Any]:
    claude_path = repo_root / "CLAUDE.md"
    if claude_path.is_symlink():
        target = os.readlink(claude_path)
        if target == "AGENTS.md":
            return {"path": str(claude_path), "status": "symlink_to_agents", "target": target}
        if profile.claude_symlink:
            findings.append(
                Finding(
                    scope="repo",
                    severity="warning",
                    component="claude-md",
                    message=f"CLAUDE.md is a symlink to '{target}' instead of 'AGENTS.md'.",
                    remediation="gardenia repo init --force-claude-symlink",
                )
            )
        return {"path": str(claude_path), "status": "symlink_other", "target": target}

    if claude_path.exists():
        if profile.claude_symlink:
            findings.append(
                Finding(
                    scope="repo",
                    severity="warning",
                    component="claude-md",
                    message="CLAUDE.md is a regular file rather than a symlink to AGENTS.md.",
                    remediation="gardenia repo init --force-claude-symlink  # or set claude_symlink = false in gardenia.toml",
                )
            )
        return {"path": str(claude_path), "status": "regular_file", "target": None}

    if profile.claude_symlink:
        findings.append(
            Finding(
                scope="repo",
                severity="warning",
                component="claude-md",
                message="CLAUDE.md symlink to AGENTS.md is missing.",
                remediation="gardenia repo init",
            )
        )
    return {"path": str(claude_path), "status": "missing", "target": None}


def _inspect_howhard_local_store(git_common_dir: Path, findings: list[Finding]) -> dict[str, Any]:
    store_dir = git_common_dir / "howhard"
    exists = store_dir.is_dir()
    store_json = (store_dir / "store.json").is_file()
    payloads_sqlite = (store_dir / "payloads.sqlite3").is_file()

    requests_dir = store_dir / "requests"
    decisions_dir = store_dir / "decisions"
    legacy_requests = len(list(requests_dir.glob("*.json"))) if requests_dir.is_dir() else 0
    legacy_decisions = len(list(decisions_dir.glob("*.json"))) if decisions_dir.is_dir() else 0
    has_legacy = (legacy_requests + legacy_decisions) > 0

    if has_legacy:
        findings.append(
            Finding(
                scope="repo",
                severity="warning",
                component="howhard-store",
                message=(
                    f"Pre-4.0 loose howhard JSON files detected under {store_dir} "
                    f"(requests/*.json: {legacy_requests}, decisions/*.json: {legacy_decisions}). "
                    "gardenia never deletes or mutates these files automatically."
                ),
                remediation="Run howhard's migrate-payloads.py migration script against this repository's howhard store.",
            )
        )

    return {
        "path": str(store_dir),
        "exists": exists,
        "store_json": store_json,
        "payloads_sqlite": payloads_sqlite,
        "legacy_requests_json_count": legacy_requests,
        "legacy_decisions_json_count": legacy_decisions,
        "has_legacy_loose_json": has_legacy,
    }


def _inspect_agroute_local_store(
    repo_root: Path,
    git_common_dir: Path,
    env: Mapping[str, str],
    findings: list[Finding],
) -> dict[str, Any]:
    store_dir = git_common_dir / "agroute"
    exists = store_dir.is_dir()

    agr_path = shutil.which("agr", path=env.get("PATH"))
    if agr_path is None:
        return {
            "path": str(store_dir),
            "exists": exists,
            "prices_pending_checked": False,
            "pending_price_count": None,
            "pending_models": [],
            "prices_pending_error": "agr-not-on-path",
        }

    result = run_cmd([agr_path, "prices", "pending", "--json"], cwd=repo_root, timeout=DEFAULT_TIMEOUT_SECONDS, env=env)
    if not result.ok:
        detail = result.stderr.strip() or result.stdout.strip() or f"exit code {result.returncode}"
        return {
            "path": str(store_dir),
            "exists": exists,
            "prices_pending_checked": False,
            "pending_price_count": None,
            "pending_models": [],
            "prices_pending_error": detail,
        }

    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        return {
            "path": str(store_dir),
            "exists": exists,
            "prices_pending_checked": False,
            "pending_price_count": None,
            "pending_models": [],
            "prices_pending_error": f"invalid JSON: {exc}",
        }

    pending_count = payload.get("pending_count")
    if not isinstance(pending_count, int):
        pending_count = 0
    pending_models: list[str] = []
    price_request = payload.get("price_request")
    if isinstance(price_request, dict):
        raw_models = price_request.get("models") or price_request.get("unknown_models") or []
        if isinstance(raw_models, list):
            for item in raw_models:
                if isinstance(item, str):
                    pending_models.append(item)
                elif isinstance(item, dict):
                    key = item.get("catalog_key") or item.get("model") or item.get("slug")
                    if key:
                        pending_models.append(str(key))

    if pending_count > 0:
        model_note = f" ({', '.join(pending_models)})" if pending_models else ""
        findings.append(
            Finding(
                scope="repo",
                severity="warning",
                component="agroute-prices",
                message=f"{pending_count} discovered model(s) have pending unresolved prices in local agroute state{model_note}.",
                remediation="Run 'agr prices pending --json', look up official provider pricing, and run 'agr prices resolve --file <path> --json'.",
            )
        )

    return {
        "path": str(store_dir),
        "exists": exists,
        "prices_pending_checked": True,
        "pending_price_count": pending_count,
        "pending_models": pending_models,
        "prices_pending_error": None,
    }


def check_repo(
    repo_path: str | os.PathLike[str] | None = None,
    *,
    strict: bool = False,
    env: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Perform read-only inspection of tracked repository settings and local ``.git/`` state."""

    effective_env = _effective_env(env)
    git_info = resolve_git_repo(repo_path, env=effective_env)
    findings: list[Finding] = []

    if git_info.is_linked_worktree:
        findings.append(
            Finding(
                scope="repo",
                severity="info",
                component="worktree",
                message=(
                    f"Target checkout {git_info.root} is a linked Git worktree sharing common Git state "
                    f"at {git_info.git_common_dir}."
                ),
                remediation="Run 'gardenia repo init' in the primary checkout (or pass --allow-worktree explicitly).",
            )
        )

    cfg = load_repo_config(git_info.root)
    profile: RepoProfile = cfg["profile"]
    if not cfg["valid"]:
        findings.append(
            Finding(
                scope="repo",
                severity="error",
                component="gardenia-config",
                message=f"Invalid repository gardenia configuration ({cfg['path']}): {cfg['error']}",
                remediation=f"Fix configuration errors in {cfg['path']}",
            )
        )
    if cfg.get("duplicate_sources"):
        findings.append(
            Finding(
                scope="repo",
                severity="warning",
                component="gardenia-config",
                message="Both gardenia.toml and [tool.gardenia] in pyproject.toml exist; gardenia.toml takes precedence.",
                remediation="Keep repository profile settings in a single file (gardenia.toml recommended).",
            )
        )

    agents_path = git_info.root / "AGENTS.md"
    agents_exists = agents_path.is_file()
    agents_text = agents_path.read_text(encoding="utf-8") if agents_exists else ""
    extracted_block, block_malformed = extract_managed_block(agents_text) if agents_exists else (None, False)
    expected_block = render_managed_block(profile)
    in_sync: bool | None = None
    if not agents_exists:
        findings.append(
            Finding(
                scope="repo",
                severity="error",
                component="agents-md",
                message="Tracked AGENTS.md file is missing from the repository root.",
                remediation="gardenia repo init",
            )
        )
    elif block_malformed:
        in_sync = False
        findings.append(
            Finding(
                scope="repo",
                severity="error",
                component="agents-md",
                message=f"AGENTS.md has malformed '{MANAGED_BEGIN}' / '{MANAGED_END}' markers.",
                remediation="Repair the marker pair in AGENTS.md, then run: gardenia repo init",
            )
        )
    elif extracted_block is None:
        in_sync = False
        findings.append(
            Finding(
                scope="repo",
                severity="warning",
                component="agents-md",
                message="AGENTS.md exists but does not contain a gardenia managed block.",
                remediation="gardenia repo init",
            )
        )
    else:
        in_sync = extracted_block == expected_block
        if not in_sync:
            findings.append(
                Finding(
                    scope="repo",
                    severity="warning",
                    component="agents-md",
                    message="AGENTS.md managed block has drifted from the repository gardenia profile.",
                    remediation="gardenia repo init",
                )
            )

    lower_agents = agents_text.lower()
    layers_referenced = {
        "agrein": "agrein" in lower_agents,
        "agroute": ("agroute" in lower_agents) or ("agr " in lower_agents),
        "howhard": "howhard" in lower_agents,
        "agcoord": ("agcoord" in lower_agents) or ("agc " in lower_agents),
    }

    claude_md = _inspect_claude_md(git_info.root, profile, findings)
    howhard_toml = _inspect_optional_toml(git_info.root / "howhard.toml", "howhard-toml", findings)
    agroute_toml = _inspect_optional_toml(git_info.root / "agroute.toml", "agroute-toml", findings)

    howhard_store = _inspect_howhard_local_store(git_info.git_common_dir, findings)
    agroute_store = _inspect_agroute_local_store(git_info.root, git_info.git_common_dir, effective_env, findings)

    ok = compute_ok(findings, strict=strict)
    repo_data = {
        "tracked": {
            "root": str(git_info.root),
            "is_linked_worktree": git_info.is_linked_worktree,
            "config": {
                "source": cfg["source"],
                "path": cfg["path"],
                "valid": cfg["valid"],
                "values": cfg["values"],
                "error": cfg["error"],
                "duplicate_sources": cfg["duplicate_sources"],
            },
            "agents_md": {
                "path": str(agents_path),
                "exists": agents_exists,
                "has_managed_block": extracted_block is not None,
                "block_malformed": block_malformed,
                "in_sync": in_sync,
                "layers_referenced": layers_referenced,
            },
            "claude_md": claude_md,
            "howhard_toml": howhard_toml,
            "agroute_toml": agroute_toml,
        },
        "local_state": {
            "git_dir": str(git_info.git_dir),
            "git_common_dir": str(git_info.git_common_dir),
            "howhard_store": howhard_store,
            "agroute_store": agroute_store,
        },
    }

    return {
        "format": CHECK_FORMAT,
        "version": __version__,
        "scope": "repo",
        "ok": ok,
        "strict": strict,
        "repo": repo_data,
        "findings": [f.to_dict() for f in findings],
        "_findings": findings,
    }


def render_repo(
    *,
    repo_path: str | os.PathLike[str] | None = None,
    gate: str | None = None,
    cpu: int | None = None,
    memory_gb: float | None = None,
    tmpfs_gb: float | None = None,
    no_overflow: bool | None = None,
    routing: str | None = None,
    agcoord: str | None = None,
    docs_exemption: bool | None = None,
    claude_symlink: bool | None = None,
    env: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Render the managed ``AGENTS.md`` block using optional repo config + CLI overrides."""

    effective_env = _effective_env(env)
    base_profile = RepoProfile()
    config_source = "defaults"
    if repo_path is not None:
        git_info = resolve_git_repo(repo_path, env=effective_env)
        cfg = load_repo_config(git_info.root)
        if not cfg["valid"]:
            raise GardeniaError(f"Invalid repository config ({cfg['path']}): {cfg['error']}", exit_code=2)
        base_profile = cfg["profile"]
        config_source = cfg["source"]
    else:
        git_info = try_resolve_git_repo(None, env=effective_env)
        if git_info is not None:
            cfg = load_repo_config(git_info.root)
            if not cfg["valid"]:
                raise GardeniaError(f"Invalid repository config ({cfg['path']}): {cfg['error']}", exit_code=2)
            base_profile = cfg["profile"]
            config_source = cfg["source"]

    profile = merge_profile_overrides(
        base_profile,
        gate=gate,
        cpu=cpu,
        memory_gb=memory_gb,
        tmpfs_gb=tmpfs_gb,
        no_overflow=no_overflow,
        routing=routing,
        agcoord=agcoord,
        docs_exemption=docs_exemption,
        claude_symlink=claude_symlink,
    )
    block = render_managed_block(profile)
    return {
        "scope": "repo",
        "config_source": config_source,
        "profile": profile.to_dict(),
        "managed_block": block,
    }


def init_repo(
    *,
    repo_path: str | os.PathLike[str] | None = None,
    gate: str | None = None,
    cpu: int | None = None,
    memory_gb: float | None = None,
    tmpfs_gb: float | None = None,
    no_overflow: bool | None = None,
    routing: str | None = None,
    agcoord: str | None = None,
    docs_exemption: bool | None = None,
    claude_symlink: bool | None = None,
    force_claude_symlink: bool = False,
    save_config: bool = False,
    allow_worktree: bool = False,
    dry_run: bool = False,
    env: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Initialize or update tracked repository files (``AGENTS.md``, ``CLAUDE.md``, ``gardenia.toml``)."""

    effective_env = _effective_env(env)
    git_info = resolve_git_repo(repo_path, env=effective_env)
    if git_info.is_linked_worktree and not allow_worktree:
        raise GardeniaError(
            f"Refusing to run 'repo init' inside linked Git worktree {git_info.root}. "
            "Run in the primary checkout or pass --allow-worktree explicitly.",
            exit_code=2,
        )

    cfg = load_repo_config(git_info.root)
    if not cfg["valid"]:
        raise GardeniaError(f"Invalid repository config ({cfg['path']}): {cfg['error']}", exit_code=2)

    profile = merge_profile_overrides(
        cfg["profile"],
        gate=gate,
        cpu=cpu,
        memory_gb=memory_gb,
        tmpfs_gb=tmpfs_gb,
        no_overflow=no_overflow,
        routing=routing,
        agcoord=agcoord,
        docs_exemption=docs_exemption,
        claude_symlink=claude_symlink,
    )

    block = render_managed_block(profile)
    agents_path = git_info.root / "AGENTS.md"
    existing_agents = agents_path.read_text(encoding="utf-8") if agents_path.is_file() else None
    updated_agents = apply_managed_block(existing_agents, block)

    actions: list[PlannedAction] = []
    warnings: list[str] = []

    # 1. AGENTS.md managed block.
    if existing_agents == updated_agents:
        actions.append(
            PlannedAction(
                kind="skip",
                component="agents-md",
                description="AGENTS.md managed block is already up to date.",
                path=str(agents_path),
                status="skipped",
            )
        )
    else:
        desc = "Create AGENTS.md with gardenia managed block." if existing_agents is None else "Update gardenia managed block in AGENTS.md."
        actions.append(
            PlannedAction(
                kind="write_file",
                component="agents-md",
                description=desc,
                path=str(agents_path),
            )
        )

    # 2. CLAUDE.md -> AGENTS.md symlink.
    claude_path = git_info.root / "CLAUDE.md"
    create_symlink = False
    replace_symlink = False
    if profile.claude_symlink:
        if claude_path.is_symlink() and os.readlink(claude_path) == "AGENTS.md":
            actions.append(
                PlannedAction(
                    kind="skip",
                    component="claude-md",
                    description="CLAUDE.md symlink to AGENTS.md is already in place.",
                    path=str(claude_path),
                    status="skipped",
                )
            )
        elif not claude_path.exists() and not claude_path.is_symlink():
            create_symlink = True
            actions.append(
                PlannedAction(
                    kind="symlink",
                    component="claude-md",
                    description="Create symlink CLAUDE.md -> AGENTS.md.",
                    path=str(claude_path),
                )
            )
        elif force_claude_symlink:
            create_symlink = True
            replace_symlink = True
            actions.append(
                PlannedAction(
                    kind="symlink",
                    component="claude-md",
                    description="Replace existing CLAUDE.md with symlink CLAUDE.md -> AGENTS.md.",
                    path=str(claude_path),
                )
            )
        else:
            warning_msg = (
                "Skipping CLAUDE.md symlink because a non-matching file or symlink already exists "
                "(pass --force-claude-symlink to replace it)."
            )
            warnings.append(warning_msg)
            actions.append(
                PlannedAction(
                    kind="skip",
                    component="claude-md",
                    description=warning_msg,
                    path=str(claude_path),
                    status="skipped",
                )
            )
    else:
        actions.append(
            PlannedAction(
                kind="skip",
                component="claude-md",
                description="CLAUDE.md symlink management disabled by profile.",
                path=str(claude_path),
                status="skipped",
            )
        )

    # 3. Optional gardenia.toml persistence.
    gardenia_toml_path = git_info.root / "gardenia.toml"
    rendered_toml: str | None = None
    if save_config:
        rendered_toml = render_gardenia_toml(profile)
        existing_toml = gardenia_toml_path.read_text(encoding="utf-8") if gardenia_toml_path.is_file() else None
        if existing_toml == rendered_toml:
            actions.append(
                PlannedAction(
                    kind="skip",
                    component="gardenia-config",
                    description="gardenia.toml is already up to date.",
                    path=str(gardenia_toml_path),
                    status="skipped",
                )
            )
        else:
            actions.append(
                PlannedAction(
                    kind="write_file",
                    component="gardenia-config",
                    description="Write tracked repository profile to gardenia.toml.",
                    path=str(gardenia_toml_path),
                )
            )

    if not dry_run:
        for action in actions:
            if action.kind == "skip":
                continue
            if action.component == "agents-md" and action.kind == "write_file":
                agents_path.write_text(updated_agents, encoding="utf-8")
                action.status = "executed"
            elif action.component == "claude-md" and action.kind == "symlink" and create_symlink:
                if replace_symlink and (claude_path.exists() or claude_path.is_symlink()):
                    claude_path.unlink()
                claude_path.symlink_to("AGENTS.md")
                action.status = "executed"
            elif action.component == "gardenia-config" and action.kind == "write_file" and rendered_toml is not None:
                gardenia_toml_path.write_text(rendered_toml, encoding="utf-8")
                action.status = "executed"

    return {
        "scope": "repo",
        "ok": True,
        "dry_run": dry_run,
        "root": str(git_info.root),
        "is_linked_worktree": git_info.is_linked_worktree,
        "profile": profile.to_dict(),
        "managed_block": block,
        "warnings": warnings,
        "actions": [action.to_dict() for action in actions],
    }
