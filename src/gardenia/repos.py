"""Host repository registry, multi-repository fleet check, and isolated-worktree upgrade sweep."""

from __future__ import annotations

from collections import deque
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import shutil
import sys
import tempfile
import tomllib
from typing import Any, Mapping, Sequence

from gardenia import __version__
from gardenia.host import BROKER_PROBE_TIMEOUT_SECONDS
from gardenia.models import (
    DEFAULT_TIMEOUT_SECONDS,
    DEFAULT_TMPFS_INODES,
    MANAGED_BEGIN,
    REPOS_CHECK_FORMAT,
    REPOS_LIST_FORMAT,
    REPOS_REGISTRY_FORMAT,
    REPOS_UPGRADE_FORMAT,
    Finding,
    GardeniaError,
    is_finite_number,
    run_cmd,
)
from gardenia.repo import (
    RepoProfile,
    check_repo,
    extract_managed_block,
    init_repo,
    load_repo_config,
    render_managed_block,
    resolve_git_repo,
    try_resolve_git_repo,
)

REGISTRY_ALLOWED_KEYS = frozenset({"format", "repos"})
SKIP_SCAN_DIR_NAMES = frozenset({"node_modules", "__pycache__"})
UPGRADE_CMD_TIMEOUT_SECONDS = 300.0
GATE_TIMEOUT_SECONDS = 7200.0
DOCS_EXEMPT_ROOT_FILES = frozenset({"README.md", "CHANGELOG.md", "AGENTS.md", "CLAUDE.md"})
_PYPROJECT_EXTRA_KEYS: tuple[str, ...] = ("test", "docs", "dev")
_PYPROJECT_GROUP_KEYS: tuple[str, ...] = ("dev", "test", "docs")

_PACKAGE_SPEC_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._@+=-]*$")
_URL_RE = re.compile(r"https?://\S+")
_TRAILING_INT_RE = re.compile(r"/(\d+)/?$")


def _effective_env(env: Mapping[str, str] | None) -> dict[str, str]:
    return dict(os.environ if env is None else env)


def _effective_home(env: Mapping[str, str]) -> Path:
    raw = env.get("HOME")
    if raw:
        return Path(raw)
    return Path.home()


def effective_repos_config_path(env: Mapping[str, str] | None = None) -> Path:
    """Resolve the host repository registry file path from environment variables."""

    effective_env = _effective_env(env)
    override = effective_env.get("GARDENIA_REPOS_CONFIG")
    if override and override.strip():
        return Path(override.strip()).expanduser()
    xdg = effective_env.get("XDG_CONFIG_HOME")
    if xdg and xdg.strip():
        return Path(xdg.strip()).expanduser() / "gardenia" / "repos.toml"
    return _effective_home(effective_env) / ".config" / "gardenia" / "repos.toml"


def effective_worktree_root(
    worktree_root: str | os.PathLike[str] | None = None,
    env: Mapping[str, str] | None = None,
) -> Path:
    """Resolve base directory for temporary isolated upgrade worktrees."""

    if worktree_root is not None:
        return Path(worktree_root).expanduser().resolve()
    effective_env = _effective_env(env)
    override = effective_env.get("GARDENIA_WORKTREE_ROOT")
    if override and override.strip():
        return Path(override.strip()).expanduser().resolve()
    var_tmp = Path("/var/tmp")
    if var_tmp.is_dir() and os.access(var_tmp, os.W_OK):
        return var_tmp
    return Path(tempfile.gettempdir())


def effective_log_dir(
    log_dir: str | os.PathLike[str] | None = None,
    env: Mapping[str, str] | None = None,
) -> Path:
    """Resolve directory where failed upgrade gate logs are persisted."""

    if log_dir is not None:
        return Path(log_dir).expanduser().resolve()
    effective_env = _effective_env(env)
    override = effective_env.get("GARDENIA_LOG_DIR")
    if override and override.strip():
        return Path(override.strip()).expanduser().resolve()
    xdg_state = effective_env.get("XDG_STATE_HOME")
    if xdg_state and xdg_state.strip():
        return Path(xdg_state.strip()).expanduser() / "gardenia" / "logs"
    return _effective_home(effective_env) / ".local" / "state" / "gardenia" / "logs"


def _canonicalize_path_str(raw: str | os.PathLike[str]) -> str:
    return str(Path(raw).expanduser().resolve(strict=False))


def _canonicalize_repos(repos: Sequence[str | os.PathLike[str]]) -> list[str]:
    return sorted({_canonicalize_path_str(p) for p in repos})


def load_repos_registry(env: Mapping[str, str] | None = None) -> dict[str, Any]:
    """Load and validate ``repos.toml`` from the host config path."""

    path = effective_repos_config_path(env)
    if not path.exists():
        return {
            "path": str(path),
            "exists": False,
            "format": REPOS_REGISTRY_FORMAT,
            "repos": [],
        }
    if path.is_dir():
        raise GardeniaError(f"Host repository registry path is a directory: {path}", exit_code=2)

    try:
        raw_text = path.read_text(encoding="utf-8")
        data = tomllib.loads(raw_text)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise GardeniaError(f"Invalid host repository registry ({path}): {exc}", exit_code=2) from exc

    unknown = sorted(set(data.keys()) - REGISTRY_ALLOWED_KEYS)
    if unknown:
        raise GardeniaError(
            f"Unknown key(s) in {path}: {', '.join(unknown)}. Allowed keys: format, repos.",
            exit_code=2,
        )

    fmt = data.get("format")
    if fmt != REPOS_REGISTRY_FORMAT:
        raise GardeniaError(
            f"Invalid 'format' in {path}: expected {REPOS_REGISTRY_FORMAT!r}, got {fmt!r}.",
            exit_code=2,
        )

    raw_repos = data.get("repos")
    if not isinstance(raw_repos, list) or any(
        not isinstance(item, str) or not item.strip() for item in raw_repos
    ):
        raise GardeniaError(
            f"Invalid 'repos' in {path}: expected a list of non-empty repository path strings.",
            exit_code=2,
        )

    canonical = _canonicalize_repos(raw_repos)
    return {
        "path": str(path),
        "exists": True,
        "format": REPOS_REGISTRY_FORMAT,
        "repos": canonical,
    }


def render_repos_registry_toml(repos: Sequence[str | os.PathLike[str]]) -> str:
    """Render canonical, sorted ``gardenia-repos-v1`` TOML."""

    canonical = _canonicalize_repos(repos)
    lines = [f"format = {json.dumps(REPOS_REGISTRY_FORMAT)}"]
    if canonical:
        lines.append("repos = [")
        for entry in canonical:
            lines.append(f"  {json.dumps(entry)},")
        lines.append("]")
    else:
        lines.append("repos = []")
    lines.append("")
    return "\n".join(lines)


def save_repos_registry(
    repos: Sequence[str | os.PathLike[str]],
    *,
    env: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Atomically write canonical sorted ``repos.toml`` via sibling temp file + ``os.replace``."""

    path = effective_repos_config_path(env)
    canonical = _canonicalize_repos(repos)
    content = render_repos_registry_toml(canonical)
    path.parent.mkdir(parents=True, exist_ok=True)

    tmp_path: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as fh:
            tmp_path = fh.name
            fh.write(content)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp_path, path)
        tmp_path = None
    finally:
        if tmp_path is not None:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass

    return {
        "path": str(path),
        "exists": True,
        "format": REPOS_REGISTRY_FORMAT,
        "repos": canonical,
    }


def is_gardenia_managed(repo_root: Path) -> bool:
    """Return True if ``repo_root`` contains gardenia config or managed AGENTS.md markers."""

    if (repo_root / "gardenia.toml").is_file():
        return True

    pyproject = repo_root / "pyproject.toml"
    if pyproject.is_file():
        try:
            py_data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
        except (OSError, tomllib.TOMLDecodeError):
            pass
        else:
            tool_section = py_data.get("tool")
            if isinstance(tool_section, dict) and "gardenia" in tool_section:
                return True

    agents_md = repo_root / "AGENTS.md"
    if agents_md.is_file():
        try:
            text = agents_md.read_text(encoding="utf-8", errors="replace")
        except OSError:
            pass
        else:
            if MANAGED_BEGIN in text:
                return True

    return False


def discover_managed_repos(
    root: str | os.PathLike[str],
    *,
    max_depth: int = 2,
    env: Mapping[str, str] | None = None,
) -> list[Path]:
    """Discover primary Git checkouts managed by gardenia under ``root`` up to ``max_depth``."""

    if isinstance(max_depth, bool) or not isinstance(max_depth, int) or max_depth < 0:
        raise GardeniaError("--max-depth must be an integer >= 0.", exit_code=2)

    effective_env = _effective_env(env)
    root_path = Path(root).expanduser().resolve()
    if not root_path.exists():
        raise GardeniaError(f"Root directory does not exist: {root_path}", exit_code=2)
    if not root_path.is_dir():
        raise GardeniaError(f"Root path is not a directory: {root_path}", exit_code=2)

    discovered: set[Path] = set()
    visited: set[Path] = {root_path}
    queue: deque[tuple[Path, int]] = deque([(root_path, 0)])

    while queue:
        current, depth = queue.popleft()
        git_marker = current / ".git"
        if git_marker.exists():
            info = try_resolve_git_repo(current, env=effective_env)
            if info is not None:
                if not info.is_linked_worktree and info.root == current and is_gardenia_managed(info.root):
                    discovered.add(info.root)
                # Do not descend inside a Git repository checkout or linked worktree.
                continue

        if depth >= max_depth:
            continue

        try:
            entries = sorted(current.iterdir(), key=lambda p: p.name)
        except OSError:
            continue

        for entry in entries:
            name = entry.name
            if name.startswith(".") or name in SKIP_SCAN_DIR_NAMES:
                continue
            try:
                if not entry.is_dir():
                    continue
                resolved = entry.resolve()
            except OSError:
                continue
            if resolved in visited:
                continue
            visited.add(resolved)
            queue.append((resolved, depth + 1))

    return sorted(discovered)


def _inspect_repo_summary(
    path_str: str,
    *,
    registered: bool,
    env: Mapping[str, str],
) -> tuple[dict[str, Any], list[str]]:
    path = Path(path_str)
    exists = path.is_dir()
    is_git_repo = False
    is_linked_worktree = False
    branch: str | None = None
    clean: bool | None = None
    managed = False
    agents_md_in_sync: bool | None = None
    strict_issues: list[str] = []

    if not exists:
        strict_issues.append("missing")
    else:
        git_info = try_resolve_git_repo(path, env=env)
        if git_info is None or git_info.root != path.resolve():
            strict_issues.append("not_git_repo")
        else:
            is_git_repo = True
            is_linked_worktree = git_info.is_linked_worktree
            if is_linked_worktree:
                strict_issues.append("linked_worktree")

            branch_res = run_cmd(
                ["git", "symbolic-ref", "--short", "-q", "HEAD"],
                cwd=git_info.root,
                timeout=DEFAULT_TIMEOUT_SECONDS,
                env=env,
            )
            branch = branch_res.stdout.strip() if (branch_res.ok and branch_res.stdout.strip()) else "HEAD"

            status_res = run_cmd(
                ["git", "status", "--porcelain"],
                cwd=git_info.root,
                timeout=DEFAULT_TIMEOUT_SECONDS,
                env=env,
            )
            clean = (status_res.stdout == "") if status_res.ok else None

            managed = is_gardenia_managed(git_info.root)
            if not managed:
                strict_issues.append("unmanaged")

            agents_path = git_info.root / "AGENTS.md"
            if agents_path.is_file():
                cfg = load_repo_config(git_info.root)
                if cfg["valid"]:
                    try:
                        agents_text = agents_path.read_text(encoding="utf-8")
                    except OSError:
                        agents_md_in_sync = False
                    else:
                        extracted, malformed = extract_managed_block(agents_text)
                        if malformed:
                            agents_md_in_sync = False
                        elif extracted is None:
                            agents_md_in_sync = False if managed else None
                        else:
                            expected = render_managed_block(cfg["profile"])
                            agents_md_in_sync = extracted == expected
                else:
                    agents_md_in_sync = False
            elif managed:
                agents_md_in_sync = False
            else:
                agents_md_in_sync = None

            if agents_md_in_sync is False:
                strict_issues.append("agents_md_out_of_sync")

    entry = {
        "path": path_str,
        "name": path.name,
        "registered": registered,
        "exists": exists,
        "is_git_repo": is_git_repo,
        "is_linked_worktree": is_linked_worktree,
        "branch": branch,
        "clean": clean,
        "managed": managed,
        "agents_md_in_sync": agents_md_in_sync,
    }
    return entry, strict_issues


def list_repos(
    *,
    root: str | os.PathLike[str] | None = None,
    max_depth: int = 2,
    strict: bool = False,
    env: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """List registered (and optionally discovered) repositories with read-only metadata."""

    effective_env = _effective_env(env)
    registry = load_repos_registry(effective_env)
    registered_set = set(registry["repos"])

    discovered_set: set[str] = set()
    resolved_root: str | None = None
    if root is not None:
        resolved_root = str(Path(root).expanduser().resolve())
        discovered_set = {str(p) for p in discover_managed_repos(root, max_depth=max_depth, env=effective_env)}

    all_paths = sorted(registered_set | discovered_set)
    entries: list[dict[str, Any]] = []
    issue_repo_count = 0
    for path_str in all_paths:
        entry, strict_issues = _inspect_repo_summary(
            path_str,
            registered=(path_str in registered_set),
            env=effective_env,
        )
        if strict_issues:
            issue_repo_count += 1
        entries.append(entry)

    ok = True if not strict else (issue_repo_count == 0)
    return {
        "format": REPOS_LIST_FORMAT,
        "version": __version__,
        "ok": ok,
        "strict": strict,
        "registry_path": registry["path"],
        "registry_exists": registry["exists"],
        "root": resolved_root,
        "repos": entries,
        "summary": {
            "total": len(entries),
            "registered": sum(1 for e in entries if e["registered"]),
            "managed": sum(1 for e in entries if e["managed"]),
            "in_sync": sum(1 for e in entries if e["agents_md_in_sync"] is True),
            "issues": issue_repo_count,
        },
    }


def add_repos(
    paths: Sequence[str | os.PathLike[str]] | None = None,
    *,
    env: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Add one or more primary Git repositories to ``repos.toml`` (default: current directory)."""

    effective_env = _effective_env(env)
    raw_targets: list[str | os.PathLike[str]] = list(paths) if paths else [Path.cwd()]

    resolved_roots: list[str] = []
    for raw in raw_targets:
        info = resolve_git_repo(raw, env=effective_env)
        if info.is_linked_worktree:
            raise GardeniaError(
                f"Refusing to register linked Git worktree {info.root}. Register the primary checkout instead.",
                exit_code=2,
            )
        root_str = str(info.root)
        if root_str not in resolved_roots:
            resolved_roots.append(root_str)

    registry = load_repos_registry(effective_env)
    existing_set = set(registry["repos"])
    added = sorted(r for r in resolved_roots if r not in existing_set)
    already_registered = sorted(r for r in resolved_roots if r in existing_set)
    updated = sorted(existing_set | set(resolved_roots))

    saved = save_repos_registry(updated, env=effective_env)
    return {
        "format": REPOS_REGISTRY_FORMAT,
        "version": __version__,
        "ok": True,
        "registry_path": saved["path"],
        "added": added,
        "already_registered": already_registered,
        "repos": saved["repos"],
    }


def remove_repos(
    paths: Sequence[str | os.PathLike[str]],
    *,
    env: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Remove repository path(s) from ``repos.toml`` idempotently."""

    if not paths:
        raise GardeniaError("At least one repository PATH is required.", exit_code=2)

    effective_env = _effective_env(env)
    registry = load_repos_registry(effective_env)
    existing_set = set(registry["repos"])

    targets = _canonicalize_repos(paths)
    removed = sorted(p for p in targets if p in existing_set)
    not_found = sorted(p for p in targets if p not in existing_set)
    remaining = sorted(existing_set - set(removed))

    if removed:
        saved = save_repos_registry(remaining, env=effective_env)
        final_repos = saved["repos"]
    else:
        final_repos = registry["repos"]

    return {
        "format": REPOS_REGISTRY_FORMAT,
        "version": __version__,
        "ok": True,
        "registry_path": registry["path"],
        "removed": removed,
        "not_found": not_found,
        "repos": final_repos,
    }


def scan_repos(
    *,
    root: str | os.PathLike[str],
    max_depth: int = 2,
    dry_run: bool = False,
    env: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Scan ``root`` for primary gardenia-managed Git checkouts and register them."""

    effective_env = _effective_env(env)
    resolved_root = Path(root).expanduser().resolve()
    discovered_paths = discover_managed_repos(resolved_root, max_depth=max_depth, env=effective_env)
    discovered = [str(p) for p in discovered_paths]

    registry = load_repos_registry(effective_env)
    existing_set = set(registry["repos"])
    added = sorted(p for p in discovered if p not in existing_set)
    already_registered = sorted(p for p in discovered if p in existing_set)
    updated = sorted(existing_set | set(discovered))

    if not dry_run and added:
        saved = save_repos_registry(updated, env=effective_env)
        final_repos = saved["repos"]
    else:
        final_repos = updated if not dry_run else registry["repos"]

    return {
        "format": REPOS_REGISTRY_FORMAT,
        "version": __version__,
        "ok": True,
        "dry_run": dry_run,
        "registry_path": registry["path"],
        "root": str(resolved_root),
        "max_depth": max_depth,
        "discovered": discovered,
        "added": added,
        "already_registered": already_registered,
        "repos": final_repos,
    }


def prune_repos(
    *,
    dry_run: bool = False,
    env: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Remove entries from ``repos.toml`` that no longer exist or are not primary Git checkouts."""

    effective_env = _effective_env(env)
    registry = load_repos_registry(effective_env)
    kept: list[str] = []
    removed: list[str] = []

    for path_str in registry["repos"]:
        p = Path(path_str)
        if not p.is_dir():
            removed.append(path_str)
            continue
        info = try_resolve_git_repo(p, env=effective_env)
        if info is None or info.is_linked_worktree or info.root != p.resolve():
            removed.append(path_str)
            continue
        kept.append(path_str)

    if not dry_run and removed:
        saved = save_repos_registry(kept, env=effective_env)
        final_repos = saved["repos"]
    else:
        final_repos = kept if not dry_run else registry["repos"]

    return {
        "format": REPOS_REGISTRY_FORMAT,
        "version": __version__,
        "ok": True,
        "dry_run": dry_run,
        "registry_path": registry["path"],
        "removed": removed,
        "kept": kept,
        "repos": final_repos,
    }


def resolve_fleet_targets(
    *,
    repos: Sequence[str | os.PathLike[str]] | None = None,
    root: str | os.PathLike[str] | None = None,
    max_depth: int = 2,
    env: Mapping[str, str] | None = None,
) -> list[str]:
    """Resolve target repositories from explicit ``--repo`` flags or registry + optional ``--root`` scan."""

    effective_env = _effective_env(env)
    if repos:
        targets = _canonicalize_repos(repos)
    else:
        registry = load_repos_registry(effective_env)
        target_set = set(registry["repos"])
        if root is not None:
            discovered = discover_managed_repos(root, max_depth=max_depth, env=effective_env)
            target_set.update(str(p) for p in discovered)
        targets = sorted(target_set)

    if not targets:
        raise GardeniaError(
            "No managed repositories found. Register repositories with 'gardenia repos add' or "
            "'gardenia repos scan --root PATH', or pass --repo PATH.",
            exit_code=2,
        )
    return targets


def check_repos(
    *,
    repos: Sequence[str | os.PathLike[str]] | None = None,
    root: str | os.PathLike[str] | None = None,
    max_depth: int = 2,
    strict: bool = False,
    env: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Run read-only ``check_repo`` across every target repository in the fleet."""

    effective_env = _effective_env(env)
    targets = resolve_fleet_targets(repos=repos, root=root, max_depth=max_depth, env=effective_env)

    repo_entries: list[dict[str, Any]] = []
    for target in targets:
        try:
            res = check_repo(target, strict=strict, env=effective_env)
        except GardeniaError as exc:
            finding = Finding(
                scope="repo",
                severity="error",
                component="repo",
                message=exc.message,
                remediation="Verify the repository path or clean stale entries with: gardenia repos prune",
            )
            repo_entries.append(
                {
                    "path": target,
                    "name": Path(target).name,
                    "ok": False,
                    "error": exc.message,
                    "repo": None,
                    "findings": [finding.to_dict()],
                    "counts": {"errors": 1, "warnings": 0, "info": 0},
                }
            )
            continue

        findings = res["findings"]
        counts = {
            "errors": sum(1 for f in findings if f["severity"] == "error"),
            "warnings": sum(1 for f in findings if f["severity"] == "warning"),
            "info": sum(1 for f in findings if f["severity"] == "info"),
        }
        repo_entries.append(
            {
                "path": res["repo"]["tracked"]["root"],
                "name": Path(res["repo"]["tracked"]["root"]).name,
                "ok": res["ok"],
                "error": None,
                "repo": res["repo"],
                "findings": findings,
                "counts": counts,
            }
        )

    all_ok = all(item["ok"] for item in repo_entries)
    return {
        "format": REPOS_CHECK_FORMAT,
        "version": __version__,
        "ok": all_ok,
        "strict": strict,
        "repos": repo_entries,
        "summary": {
            "total": len(repo_entries),
            "ok": sum(1 for item in repo_entries if item["ok"]),
            "failed": sum(1 for item in repo_entries if not item["ok"]),
            "errors": sum(item["counts"]["errors"] for item in repo_entries),
            "warnings": sum(item["counts"]["warnings"] for item in repo_entries),
        },
    }


def validate_packages(packages: Sequence[str] | None) -> list[str]:
    """Validate optional ``--package`` arguments against empty strings, whitespace, and shell metacharacters."""

    if not packages:
        return []
    validated: list[str] = []
    for raw in packages:
        if not isinstance(raw, str) or not raw or not _PACKAGE_SPEC_RE.match(raw):
            raise GardeniaError(
                f"Invalid --package value {raw!r}: must be a non-empty package name without whitespace or shell metacharacters.",
                exit_code=2,
            )
        if raw not in validated:
            validated.append(raw)
    return validated


def _is_docs_only_path(rel_path: str) -> bool:
    normalized = rel_path.replace("\\", "/").lstrip("./")
    if normalized in DOCS_EXEMPT_ROOT_FILES:
        return True
    if normalized.startswith("docs/") and normalized.endswith(".md"):
        return True
    if normalized.startswith("docs/assets/") and (normalized.endswith(".png") or normalized.endswith(".svg")):
        return True
    return False


def qualifies_for_docs_exemption(changed_files: Sequence[str]) -> bool:
    """Return True if every changed path qualifies for the documentation-only exemption."""

    return bool(changed_files) and all(_is_docs_only_path(path) for path in changed_files)


def _build_agc_resource_args(profile: RepoProfile) -> list[str]:
    args: list[str] = []
    if profile.no_overflow:
        args.append("--no-overflow")
    args.extend(["--resource", f"cpu={profile.cpu}"])
    args.extend(["--resource", f"memory={profile.memory_bytes()}"])
    tmpfs = profile.tmpfs_bytes()
    if tmpfs is not None:
        args.extend(["--resource", f"tmpfs={tmpfs}"])
        args.extend(["--resource", f"tmpfs_inodes={DEFAULT_TMPFS_INODES}"])
    return args


def _should_use_agc(profile: RepoProfile, *, cwd: Path, env: Mapping[str, str]) -> tuple[bool, str | None]:
    """Determine whether to route gate/land commands through ``agc``.

    Returns ``(use_agc, error_message_or_none)``.
    """

    if profile.agcoord == "none":
        return False, None

    agc_bin = shutil.which("agc", path=env.get("PATH"))
    if profile.agcoord == "required":
        if agc_bin is None:
            return False, "Repository profile sets agcoord='required', but 'agc' was not found on PATH."
        return True, None

    # agcoord == "auto"
    if not sys.platform.startswith("linux") or agc_bin is None:
        return False, None
    probe = run_cmd(
        [agc_bin, "--json", "list", "--limit", "1"],
        cwd=cwd,
        timeout=BROKER_PROBE_TIMEOUT_SECONDS,
        env=env,
    )
    return probe.ok, None


def _resolve_base_ref(repo_root: Path, env: Mapping[str, str]) -> tuple[str | None, bool, str | None]:
    """Resolve base ref for isolated worktree creation without modifying the primary checkout.

    Returns ``(base_ref, has_origin, error_message)``.
    """

    origin_res = run_cmd(
        ["git", "remote", "get-url", "origin"],
        cwd=repo_root,
        timeout=DEFAULT_TIMEOUT_SECONDS,
        env=env,
    )
    if not origin_res.ok:
        return "HEAD", False, None

    fetch_res = run_cmd(
        ["git", "fetch", "--prune", "origin"],
        cwd=repo_root,
        timeout=UPGRADE_CMD_TIMEOUT_SECONDS,
        env=env,
    )
    if not fetch_res.ok:
        detail = fetch_res.stderr.strip() or fetch_res.stdout.strip() or f"exit code {fetch_res.returncode}"
        return None, True, f"git fetch --prune origin failed: {detail}"

    sym_res = run_cmd(
        ["git", "symbolic-ref", "--quiet", "--short", "refs/remotes/origin/HEAD"],
        cwd=repo_root,
        timeout=DEFAULT_TIMEOUT_SECONDS,
        env=env,
    )
    candidates: list[str] = []
    if sym_res.ok and sym_res.stdout.strip():
        candidates.append(sym_res.stdout.strip())
    candidates.extend(["origin/main", "origin/master", "HEAD"])

    for ref in candidates:
        verify = run_cmd(
            ["git", "rev-parse", "--verify", "--quiet", ref],
            cwd=repo_root,
            timeout=DEFAULT_TIMEOUT_SECONDS,
            env=env,
        )
        if verify.ok:
            return ref, True, None

    return None, True, "Could not resolve a valid base Git ref (origin/HEAD, origin/main, origin/master, or HEAD)."


def _normalize_dep_group_name(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def _collect_dependency_group_reqs(
    data: Mapping[str, Any],
    *,
    pyproject_path: Path,
    group_names: Sequence[str] = _PYPROJECT_GROUP_KEYS,
) -> list[str]:
    dep_groups = data.get("dependency-groups")
    if dep_groups is None:
        return []
    if not isinstance(dep_groups, dict):
        raise GardeniaError(
            f"Invalid [dependency-groups] in {pyproject_path}: expected a TOML table.",
            exit_code=2,
        )

    normalized_groups: dict[str, Any] = {}
    for raw_key, raw_items in dep_groups.items():
        if not isinstance(raw_key, str):
            raise GardeniaError(
                f"Invalid group name {raw_key!r} in {pyproject_path} [dependency-groups].",
                exit_code=2,
            )
        norm_key = _normalize_dep_group_name(raw_key)
        if norm_key in normalized_groups:
            raise GardeniaError(
                f"Duplicate normalized dependency group {norm_key!r} in {pyproject_path}.",
                exit_code=2,
            )
        normalized_groups[norm_key] = raw_items

    collected: list[str] = []
    seen_reqs: set[str] = set()
    visited_groups: set[str] = set()
    active_groups: set[str] = set()

    def _resolve(norm_name: str) -> None:
        if norm_name in active_groups:
            raise GardeniaError(
                f"Cyclic include-group reference {norm_name!r} in {pyproject_path} [dependency-groups].",
                exit_code=2,
            )
        if norm_name in visited_groups:
            return
        if norm_name not in normalized_groups:
            raise GardeniaError(
                f"Unknown include-group {norm_name!r} in {pyproject_path} [dependency-groups].",
                exit_code=2,
            )
        items = normalized_groups[norm_name]
        if not isinstance(items, list):
            raise GardeniaError(
                f"Invalid [dependency-groups].{norm_name} in {pyproject_path}: expected a list.",
                exit_code=2,
            )

        active_groups.add(norm_name)
        for entry in items:
            if isinstance(entry, str):
                spec = entry.strip()
                if spec and spec not in seen_reqs:
                    seen_reqs.add(spec)
                    collected.append(spec)
            elif (
                isinstance(entry, dict)
                and set(entry.keys()) == {"include-group"}
                and isinstance(entry["include-group"], str)
            ):
                _resolve(_normalize_dep_group_name(entry["include-group"]))
            else:
                raise GardeniaError(
                    f"Unsupported entry {entry!r} in {pyproject_path} [dependency-groups].{norm_name}.",
                    exit_code=2,
                )
        active_groups.remove(norm_name)
        visited_groups.add(norm_name)

    for group in group_names:
        norm_target = _normalize_dep_group_name(group)
        if norm_target in normalized_groups:
            _resolve(norm_target)

    return collected


def _build_pyproject_install_args(wt_dir: Path, *, upgrade: bool = True) -> list[str]:
    pyproject_path = wt_dir / "pyproject.toml"
    try:
        data = tomllib.loads(pyproject_path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise GardeniaError(f"Invalid pyproject.toml ({pyproject_path}): {exc}", exit_code=2) from exc

    group_reqs = _collect_dependency_group_reqs(data, pyproject_path=pyproject_path)
    has_docs_reqs_file = (wt_dir / "docs" / "requirements.txt").is_file()

    if "project" in data:
        project = data.get("project")
        if not isinstance(project, dict):
            raise GardeniaError(
                f"Invalid [project] table in {pyproject_path}: expected a TOML table.",
                exit_code=2,
            )
        opt_deps = project.get("optional-dependencies")
        extras: list[str] = []
        if isinstance(opt_deps, dict):
            for key in _PYPROJECT_EXTRA_KEYS:
                if key in opt_deps:
                    extras.append(key)
        install_spec = f".[{','.join(extras)}]" if extras else "."
        args = ["-e", install_spec, *group_reqs]
        if has_docs_reqs_file and "docs" not in extras:
            args.extend(["-r", "docs/requirements.txt"])
        return args

    args = list(group_reqs)
    if has_docs_reqs_file:
        args.extend(["-r", "docs/requirements.txt"])
    if not args:
        return []
    return ["--upgrade", *args] if upgrade else args


def _provision_pyproject_venv(
    wt_dir: Path,
    *,
    venv_dir: Path,
    uv_bin: str | None,
    upgrade: bool,
    packages: Sequence[str],
    gate_env: dict[str, str],
    upgrade_actions: list[str],
    upgrade_logs: list[str],
) -> str | None:
    """Build the isolated gate virtualenv and activate it in ``gate_env``.

    ``upgrade`` controls only dependency upgrades; provisioning always happens.
    Returns an error message, or ``None`` on success.
    """

    venv_python = venv_dir / "bin" / "python"
    try:
        install_args = _build_pyproject_install_args(wt_dir, upgrade=upgrade)
    except GardeniaError as exc:
        return exc.message

    if uv_bin:
        create_venv_cmd = [uv_bin, "venv", "--seed", str(venv_dir)]
        pip_install = [uv_bin, "pip", "install", "--python", str(venv_python)]
    else:
        create_venv_cmd = [sys.executable, "-m", "venv", str(venv_dir)]
        pip_install = [str(venv_python), "-m", "pip", "install"]

    steps = [create_venv_cmd]
    if install_args:
        steps.append([*pip_install, *install_args])
    if upgrade and packages:
        steps.append([*pip_install, "--upgrade", *packages])

    for step_cmd in steps:
        step_res = run_cmd(step_cmd, cwd=wt_dir, timeout=UPGRADE_CMD_TIMEOUT_SECONDS, env=gate_env)
        upgrade_actions.append(" ".join(step_cmd))
        if step_res.stdout or step_res.stderr:
            upgrade_logs.append(f"$ {' '.join(step_cmd)}\n{step_res.stdout}{step_res.stderr}".strip())
        if not step_res.ok:
            detail = step_res.stderr.strip() or step_res.stdout.strip() or f"exit code {step_res.returncode}"
            return f"Virtualenv dependency setup failed ({step_cmd[0]}): {detail}"

    venv_bin = str(venv_dir / "bin")
    existing_path = gate_env.get("PATH", "")
    gate_env["PATH"] = f"{venv_bin}{os.pathsep}{existing_path}" if existing_path else venv_bin
    gate_env["VIRTUAL_ENV"] = str(venv_dir)
    return None


def _list_worktree_status_entries(wt_dir: Path, env: Mapping[str, str]) -> list[tuple[str, str]]:
    res = run_cmd(
        ["git", "status", "--porcelain", "-z", "--untracked-files=all"],
        cwd=wt_dir,
        timeout=DEFAULT_TIMEOUT_SECONDS,
        env=env,
    )
    if not res.ok or not res.stdout:
        return []

    tokens = res.stdout.split("\0")
    entries: list[tuple[str, str]] = []
    idx = 0
    while idx < len(tokens):
        token = tokens[idx]
        idx += 1
        if not token or len(token) < 4:
            continue
        xy = token[:2]
        path_part = token[3:]
        if path_part:
            entries.append((xy, path_part))
        if "R" in xy or "C" in xy:
            # In -z format, the next NUL token is the original source path for renames/copies.
            idx += 1
    return entries


def _list_worktree_changed_files(wt_dir: Path, env: Mapping[str, str]) -> list[str]:
    return sorted({path for _, path in _list_worktree_status_entries(wt_dir, env)})


def _clean_untracked_gate_artifacts(
    wt_dir: Path,
    pre_gate_changed: Sequence[str],
    env: Mapping[str, str],
) -> None:
    allowed = set(pre_gate_changed)
    resolved_wt = wt_dir.resolve()
    for xy, rel_path in _list_worktree_status_entries(wt_dir, env):
        if xy != "??" or rel_path in allowed:
            continue
        target = wt_dir / rel_path
        resolved_parent = target.parent.resolve()
        if resolved_parent != resolved_wt and resolved_wt not in resolved_parent.parents:
            continue
        if target.is_symlink() or target.is_file():
            target.unlink(missing_ok=True)
        elif target.is_dir():
            shutil.rmtree(target, ignore_errors=True)
        parent = target.parent
        while parent != wt_dir and wt_dir in parent.parents:
            try:
                if parent.is_dir() and not any(parent.iterdir()):
                    parent.rmdir()
                    parent = parent.parent
                else:
                    break
            except OSError:
                break


def _restore_candidate_checkout(wt_dir: Path, env: Mapping[str, str]) -> None:
    """Return the disposable worktree to its committed ``HEAD`` without moving any branch.

    Callers must commit intentional changes first; everything uncommitted is discarded
    (except ignored files).
    """

    for argv in (["git", "reset", "--quiet"], ["git", "checkout", "--", "."]):
        run_cmd(argv, cwd=wt_dir, timeout=DEFAULT_TIMEOUT_SECONDS, env=env)
    _clean_untracked_gate_artifacts(wt_dir, [], env)


def _fallback_identity_args(wt_dir: Path, env: Mapping[str, str]) -> list[str]:
    args: list[str] = []
    for key, env_var, fallback in (
        ("user.name", "GIT_AUTHOR_NAME", "gardenia"),
        ("user.email", "GIT_AUTHOR_EMAIL", "gardenia@localhost"),
    ):
        if env.get(env_var):
            continue
        res = run_cmd(["git", "config", "--get", key], cwd=wt_dir, timeout=DEFAULT_TIMEOUT_SECONDS, env=env)
        if not res.ok or not res.stdout.strip():
            args.extend(["-c", f"{key}={fallback}"])
    return args


def _upgrade_commit_title(packages: Sequence[str]) -> str:
    pkg_note = f" ({', '.join(packages)})" if packages else ""
    return f"Gardenia upgrade sweep{pkg_note}"


def _commit_candidate_branch(
    wt_dir: Path,
    *,
    repo_root: Path,
    stamp: str,
    changed_files: Sequence[str],
    commit_title: str,
    env: Mapping[str, str],
) -> tuple[str | None, str | None]:
    """Commit only ``changed_files`` onto a new candidate branch inside ``wt_dir``.

    Returns ``(created_branch, error_or_none)``. ``created_branch`` is set whenever the
    branch exists, even if staging or committing failed, so the caller can delete it.
    """

    branch_name = _unique_upgrade_branch(repo_root, f"gardenia-upgrade-{stamp}", env)
    checkout_res = run_cmd(
        ["git", "checkout", "-b", branch_name],
        cwd=wt_dir,
        timeout=DEFAULT_TIMEOUT_SECONDS,
        env=env,
    )
    if not checkout_res.ok:
        detail = checkout_res.stderr.strip() or checkout_res.stdout.strip()
        return None, f"git checkout -b {branch_name} failed: {detail}"

    add_res = run_cmd(
        ["git", "add", "--", *changed_files],
        cwd=wt_dir,
        timeout=DEFAULT_TIMEOUT_SECONDS,
        env=env,
    )
    if not add_res.ok:
        detail = add_res.stderr.strip() or add_res.stdout.strip()
        return branch_name, f"git add -- ... failed: {detail}"

    commit_res = run_cmd(
        ["git", *_fallback_identity_args(wt_dir, env), "commit", "-m", commit_title],
        cwd=wt_dir,
        timeout=30.0,
        env=env,
    )
    if not commit_res.ok:
        detail = commit_res.stderr.strip() or commit_res.stdout.strip()
        return branch_name, f"git commit failed: {detail}"
    return branch_name, None


def _unique_upgrade_branch(repo_root: Path, base_name: str, env: Mapping[str, str]) -> str:
    candidate = base_name
    suffix = 1
    while True:
        check = run_cmd(
            ["git", "show-ref", "--verify", "--quiet", f"refs/heads/{candidate}"],
            cwd=repo_root,
            timeout=DEFAULT_TIMEOUT_SECONDS,
            env=env,
        )
        if not check.ok:
            return candidate
        candidate = f"{base_name}-{suffix}"
        suffix += 1


def _extract_url(text: str) -> str | None:
    matches = _URL_RE.findall(text)
    return matches[-1] if matches else None


def _extract_trailing_number(url: str | None) -> int | None:
    if not url:
        return None
    match = _TRAILING_INT_RE.search(url.strip())
    if not match:
        return None
    return int(match.group(1))


def _format_log_excerpt(text: str, *, max_lines: int = 40, max_chars: int = 4000) -> str:
    lines = text.strip().splitlines()
    excerpt = "\n".join(lines[-max_lines:])
    if len(excerpt) > max_chars:
        excerpt = excerpt[-max_chars:]
    return excerpt


def _write_failure_log(
    *,
    log_dir: Path,
    repo_name: str,
    stamp: str,
    repo_root: Path,
    base_ref: str,
    gate_cmd: list[str],
    returncode: int | None,
    stdout: str,
    stderr: str,
    upgrade_logs: list[str],
) -> Path:
    log_dir.mkdir(parents=True, exist_ok=True)
    safe_name = re.sub(r"[^A-Za-z0-9._-]+", "_", repo_name).strip("_") or "repo"
    log_path = log_dir / f"{safe_name}-{stamp}.log"
    parts = [
        f"repo: {repo_root}",
        f"base_ref: {base_ref}",
        f"gate_command: {' '.join(gate_cmd)}",
        f"returncode: {returncode}",
        "",
    ]
    if upgrade_logs:
        parts.append("=== dependency upgrade output ===")
        parts.extend(upgrade_logs)
        parts.append("")
    if stdout:
        parts.append("=== gate stdout ===")
        parts.append(stdout)
        parts.append("")
    if stderr:
        parts.append("=== gate stderr ===")
        parts.append(stderr)
        parts.append("")
    log_path.write_text("\n".join(parts), encoding="utf-8")
    return log_path


def _file_or_reuse_failure_issue(
    *,
    wt_dir: Path,
    repo_name: str,
    base_ref: str,
    packages: list[str],
    gate: str,
    returncode: int | None,
    log_path: Path,
    log_excerpt: str,
    env: Mapping[str, str],
) -> tuple[str | None, int | None, bool | None, str | None]:
    """Create or reuse an open GitHub issue for a failed upgrade sweep.

    Returns ``(issue_url, issue_number, issue_created, error_or_none)``.
    """

    gh_bin = shutil.which("gh", path=env.get("PATH"))
    if gh_bin is None:
        return None, None, None, "'gh' CLI was not found on PATH; skipped filing GitHub issue."

    pkg_suffix = f" ({', '.join(packages)})" if packages else ""
    title = f"Gardenia upgrade sweep failed{pkg_suffix}"

    list_res = run_cmd(
        [gh_bin, "issue", "list", "--state", "open", "--json", "number,title,url"],
        cwd=wt_dir,
        timeout=30.0,
        env=env,
    )
    if list_res.ok:
        try:
            open_issues = json.loads(list_res.stdout)
        except json.JSONDecodeError:
            open_issues = []
        if isinstance(open_issues, list):
            for item in open_issues:
                if isinstance(item, dict) and item.get("title") == title:
                    url = str(item.get("url")) if item.get("url") else None
                    number = item.get("number") if isinstance(item.get("number"), int) else _extract_trailing_number(url)
                    return url, number, False, None

    body_lines = [
        f"Automated `gardenia repos upgrade` gate failed for `{repo_name}`.",
        "",
        f"- Base ref: `{base_ref}`",
        f"- Gate: `{gate}`",
        f"- Exit code: `{returncode}`",
    ]
    if packages:
        body_lines.append(f"- Requested packages: `{', '.join(packages)}`")
    body_lines.append(f"- Local failure log: `{log_path}`")
    if log_excerpt:
        body_lines.extend(["", "```text", log_excerpt, "```"])
    body = "\n".join(body_lines) + "\n"

    create_res = run_cmd(
        [gh_bin, "issue", "create", "--title", title, "--body", body],
        cwd=wt_dir,
        timeout=30.0,
        env=env,
    )
    if not create_res.ok:
        detail = create_res.stderr.strip() or create_res.stdout.strip() or f"exit code {create_res.returncode}"
        return None, None, None, f"gh issue create failed: {detail}"

    url = _extract_url(create_res.stdout)
    number = _extract_trailing_number(url)
    return url, number, True, None


def _plan_single_repo_upgrade(
    target: str,
    *,
    packages: list[str],
    sync_policy: bool,
    upgrade_locks: bool,
    land: bool,
    env: Mapping[str, str],
) -> dict[str, Any]:
    try:
        git_info = resolve_git_repo(target, env=env)
    except GardeniaError as exc:
        return {
            "path": target,
            "name": Path(target).name,
            "status": "error",
            "base_ref": None,
            "used_agcoord": None,
            "gate": None,
            "planned_steps": [],
            "changed_files": [],
            "upgrade_actions": [],
            "log_path": None,
            "log_excerpt": None,
            "issue_url": None,
            "issue_number": None,
            "issue_created": None,
            "pr_url": None,
            "pr_number": None,
            "land_method": None,
            "error": exc.message,
        }

    if git_info.is_linked_worktree:
        return {
            "path": str(git_info.root),
            "name": git_info.root.name,
            "status": "error",
            "base_ref": None,
            "used_agcoord": None,
            "gate": None,
            "planned_steps": [],
            "changed_files": [],
            "upgrade_actions": [],
            "log_path": None,
            "log_excerpt": None,
            "issue_url": None,
            "issue_number": None,
            "issue_created": None,
            "pr_url": None,
            "pr_number": None,
            "land_method": None,
            "error": f"Refusing to upgrade linked Git worktree {git_info.root}. Target the primary checkout instead.",
        }

    cfg = load_repo_config(git_info.root)
    if not cfg["valid"]:
        return {
            "path": str(git_info.root),
            "name": git_info.root.name,
            "status": "error",
            "base_ref": None,
            "used_agcoord": None,
            "gate": None,
            "planned_steps": [],
            "changed_files": [],
            "upgrade_actions": [],
            "log_path": None,
            "log_excerpt": None,
            "issue_url": None,
            "issue_number": None,
            "issue_created": None,
            "pr_url": None,
            "pr_number": None,
            "land_method": None,
            "error": f"Invalid repository config ({cfg['path']}): {cfg['error']}",
        }

    profile: RepoProfile = cfg["profile"]
    steps: list[str] = ["create_isolated_worktree"]
    if sync_policy:
        steps.append("sync_policy")
    if upgrade_locks:
        if (git_info.root / "uv.lock").is_file():
            steps.append("upgrade_uv_lock")
        if (git_info.root / "Cargo.lock").is_file():
            steps.append("upgrade_cargo_lock")
    if (git_info.root / "pyproject.toml").is_file() and not (git_info.root / "uv.lock").is_file():
        steps.append("provision_pyproject_venv")
    use_agc, agc_err = _should_use_agc(profile, cwd=git_info.root, env=env)
    steps.append(f"run_gate:{'agc' if use_agc else 'direct'}:{profile.gate}")
    if land:
        steps.append("land_verified_changes")
    steps.append("cleanup_isolated_worktree")

    return {
        "path": str(git_info.root),
        "name": git_info.root.name,
        "status": "error" if agc_err else "planned",
        "base_ref": None,
        "used_agcoord": use_agc,
        "gate": profile.gate,
        "planned_steps": steps,
        "changed_files": [],
        "upgrade_actions": [],
        "log_path": None,
        "log_excerpt": None,
        "issue_url": None,
        "issue_number": None,
        "issue_created": None,
        "pr_url": None,
        "pr_number": None,
        "land_method": None,
        "error": agc_err,
    }


def _execute_single_repo_upgrade(
    target: str,
    *,
    packages: list[str],
    sync_policy: bool,
    upgrade_locks: bool,
    land: bool,
    file_issues: bool,
    gate_timeout: float = GATE_TIMEOUT_SECONDS,
    wt_root: Path,
    resolved_log_dir: Path,
    env: Mapping[str, str],
) -> dict[str, Any]:
    def _error_result(
        path_val: str,
        message: str,
        *,
        base_ref: str | None = None,
        used_agcoord: bool | None = None,
        gate: str | None = None,
        changed_files: list[str] | None = None,
        upgrade_actions: list[str] | None = None,
        issue_url: str | None = None,
        issue_number: int | None = None,
        issue_created: bool | None = None,
        pr_url: str | None = None,
        pr_number: int | None = None,
        land_method: str | None = None,
    ) -> dict[str, Any]:
        return {
            "path": path_val,
            "name": Path(path_val).name,
            "status": "error",
            "base_ref": base_ref,
            "used_agcoord": used_agcoord,
            "gate": gate,
            "changed_files": changed_files or [],
            "upgrade_actions": upgrade_actions or [],
            "log_path": None,
            "log_excerpt": None,
            "issue_url": issue_url,
            "issue_number": issue_number,
            "issue_created": issue_created,
            "pr_url": pr_url,
            "pr_number": pr_number,
            "land_method": land_method,
            "error": message,
        }

    try:
        git_info = resolve_git_repo(target, env=env)
    except GardeniaError as exc:
        return _error_result(target, exc.message)

    repo_root = git_info.root
    repo_name = repo_root.name
    if git_info.is_linked_worktree:
        return _error_result(
            str(repo_root),
            f"Refusing to upgrade linked Git worktree {repo_root}. Target the primary checkout instead.",
        )

    base_ref, has_origin, ref_err = _resolve_base_ref(repo_root, env)
    if ref_err or base_ref is None:
        return _error_result(str(repo_root), ref_err or "Failed to resolve base ref.")

    wt_root.mkdir(parents=True, exist_ok=True)
    scratch_dir = Path(tempfile.mkdtemp(prefix=f"gardenia-upgrade-{repo_name}-", dir=wt_root))
    wt_dir = scratch_dir / "wt"
    worktree_created = False
    created_branch: str | None = None
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    upgrade_actions: list[str] = []
    upgrade_logs: list[str] = []

    try:
        add_res = run_cmd(
            ["git", "worktree", "add", "--detach", str(wt_dir), base_ref],
            cwd=repo_root,
            timeout=UPGRADE_CMD_TIMEOUT_SECONDS,
            env=env,
        )
        if not add_res.ok:
            detail = add_res.stderr.strip() or add_res.stdout.strip() or f"exit code {add_res.returncode}"
            return _error_result(str(repo_root), f"git worktree add failed: {detail}", base_ref=base_ref)
        worktree_created = True

        # 1. Optional policy sync inside isolated worktree (never registering temp worktree).
        if sync_policy:
            try:
                init_repo(repo_path=wt_dir, allow_worktree=True, register=False, env=env)
            except GardeniaError as exc:
                return _error_result(str(repo_root), f"Policy sync failed: {exc.message}", base_ref=base_ref)
            upgrade_actions.append("sync_policy")

        # 2. Load resolved RepoProfile inside the isolated worktree.
        cfg = load_repo_config(wt_dir)
        if not cfg["valid"]:
            return _error_result(
                str(repo_root),
                f"Invalid repository config ({cfg['path']}): {cfg['error']}",
                base_ref=base_ref,
                upgrade_actions=upgrade_actions,
            )
        profile: RepoProfile = cfg["profile"]

        # 3. Optional lockfile / dependency upgrades.
        gate_env = dict(env)
        uv_bin = shutil.which("uv", path=gate_env.get("PATH"))
        if upgrade_locks:
            cargo_bin = shutil.which("cargo", path=gate_env.get("PATH"))

            if (wt_dir / "uv.lock").is_file() and uv_bin:
                uv_cmd = [uv_bin, "lock"]
                if packages:
                    for pkg in packages:
                        uv_cmd.extend(["--upgrade-package", pkg])
                else:
                    uv_cmd.append("--upgrade")
                uv_res = run_cmd(uv_cmd, cwd=wt_dir, timeout=UPGRADE_CMD_TIMEOUT_SECONDS, env=gate_env)
                upgrade_actions.append(" ".join(uv_cmd))
                if uv_res.stdout or uv_res.stderr:
                    upgrade_logs.append(f"$ {' '.join(uv_cmd)}\n{uv_res.stdout}{uv_res.stderr}".strip())
                if not uv_res.ok:
                    detail = uv_res.stderr.strip() or uv_res.stdout.strip() or f"exit code {uv_res.returncode}"
                    return _error_result(
                        str(repo_root),
                        f"uv lock upgrade failed: {detail}",
                        base_ref=base_ref,
                        gate=profile.gate,
                        upgrade_actions=upgrade_actions,
                    )

            if (wt_dir / "Cargo.lock").is_file() and cargo_bin:
                cargo_cmd = [cargo_bin, "update"]
                for pkg in packages:
                    cargo_cmd.extend(["-p", pkg])
                cargo_res = run_cmd(cargo_cmd, cwd=wt_dir, timeout=UPGRADE_CMD_TIMEOUT_SECONDS, env=gate_env)
                upgrade_actions.append(" ".join(cargo_cmd))
                if cargo_res.stdout or cargo_res.stderr:
                    upgrade_logs.append(f"$ {' '.join(cargo_cmd)}\n{cargo_res.stdout}{cargo_res.stderr}".strip())
                if not cargo_res.ok:
                    detail = cargo_res.stderr.strip() or cargo_res.stdout.strip() or f"exit code {cargo_res.returncode}"
                    return _error_result(
                        str(repo_root),
                        f"cargo update failed: {detail}",
                        base_ref=base_ref,
                        gate=profile.gate,
                        upgrade_actions=upgrade_actions,
                    )

        # Capture intentional changes (policy sync + lock upgrades) BEFORE venv setup or the gate.
        changed_files = _list_worktree_changed_files(wt_dir, env)
        commit_title = _upgrade_commit_title(packages)

        # 4. Without lock upgrades, a policy sync that changed nothing (or only
        #    documentation-exempt files) has nothing for profile.gate to validate.
        skip_reason: str | None = None
        if sync_policy and not upgrade_locks:
            if not changed_files:
                skip_reason = "up_to_date"
            elif profile.docs_exemption and qualifies_for_docs_exemption(changed_files):
                skip_reason = "docs_exemption"

        use_agc = False
        if skip_reason is not None:
            upgrade_actions.append(f"skip_gate:{skip_reason}")
        else:
            # 5. Provision the test virtualenv, commit the candidate, and run profile.gate.
            if (wt_dir / "pyproject.toml").is_file() and not (wt_dir / "uv.lock").is_file():
                venv_err = _provision_pyproject_venv(
                    wt_dir,
                    venv_dir=scratch_dir / "venv",
                    uv_bin=uv_bin,
                    upgrade=upgrade_locks,
                    packages=packages,
                    gate_env=gate_env,
                    upgrade_actions=upgrade_actions,
                    upgrade_logs=upgrade_logs,
                )
                if venv_err:
                    return _error_result(
                        str(repo_root),
                        venv_err,
                        base_ref=base_ref,
                        gate=profile.gate,
                        upgrade_actions=upgrade_actions,
                    )

            # Decide agcoord policy for profile.gate.
            use_agc, agc_err = _should_use_agc(profile, cwd=wt_dir, env=gate_env)
            if agc_err:
                return _error_result(
                    str(repo_root),
                    agc_err,
                    base_ref=base_ref,
                    used_agcoord=False,
                    gate=profile.gate,
                    changed_files=changed_files,
                    upgrade_actions=upgrade_actions,
                )

            # Commit the candidate first so the gate sees a clean checkout, and drop
            # anything virtualenv setup wrote into the source tree.
            if changed_files:
                created_branch, commit_err = _commit_candidate_branch(
                    wt_dir,
                    repo_root=repo_root,
                    stamp=stamp,
                    changed_files=changed_files,
                    commit_title=commit_title,
                    env=env,
                )
                if commit_err:
                    return _error_result(
                        str(repo_root),
                        commit_err,
                        base_ref=base_ref,
                        used_agcoord=use_agc,
                        gate=profile.gate,
                        changed_files=changed_files,
                        upgrade_actions=upgrade_actions,
                    )
            _restore_candidate_checkout(wt_dir, env)

            if use_agc:
                gate_cmd = [
                    "agc",
                    "run",
                    "--label",
                    f"upgrade gate ({repo_name})",
                    *_build_agc_resource_args(profile),
                    "--",
                    "bash",
                    "-lc",
                    profile.gate,
                ]
            else:
                bash_exe = shutil.which("bash", path=gate_env.get("PATH")) or shutil.which("bash") or "/bin/bash"
                gate_cmd = [bash_exe, "-lc", profile.gate]

            gate_res = run_cmd(gate_cmd, cwd=wt_dir, timeout=gate_timeout, env=gate_env)

            # Classify outcome.
            if not gate_res.ok:
                log_path = _write_failure_log(
                    log_dir=resolved_log_dir,
                    repo_name=repo_name,
                    stamp=stamp,
                    repo_root=repo_root,
                    base_ref=base_ref,
                    gate_cmd=gate_cmd,
                    returncode=gate_res.returncode,
                    stdout=gate_res.stdout,
                    stderr=gate_res.stderr,
                    upgrade_logs=upgrade_logs,
                )
                combined_output = "\n".join(part for part in (gate_res.stdout, gate_res.stderr) if part)
                log_excerpt = _format_log_excerpt(combined_output)

                issue_url: str | None = None
                issue_number: int | None = None
                issue_created: bool | None = None
                issue_error: str | None = None
                if file_issues:
                    issue_url, issue_number, issue_created, issue_error = _file_or_reuse_failure_issue(
                        wt_dir=wt_dir,
                        repo_name=repo_name,
                        base_ref=base_ref,
                        packages=packages,
                        gate=profile.gate,
                        returncode=gate_res.returncode,
                        log_path=log_path,
                        log_excerpt=log_excerpt,
                        env=env,
                    )

                return {
                    "path": str(repo_root),
                    "name": repo_name,
                    "status": "gate_failed",
                    "base_ref": base_ref,
                    "used_agcoord": use_agc,
                    "gate": profile.gate,
                    "returncode": gate_res.returncode,
                    "changed_files": changed_files,
                    "upgrade_actions": upgrade_actions,
                    "log_path": str(log_path),
                    "log_excerpt": log_excerpt,
                    "issue_url": issue_url,
                    "issue_number": issue_number,
                    "issue_created": issue_created,
                    "pr_url": None,
                    "pr_number": None,
                    "land_method": None,
                    "error": issue_error,
                }

            # Discard anything the passing gate wrote so only the candidate commit can land.
            _restore_candidate_checkout(wt_dir, env)

        if not changed_files:
            return {
                "path": str(repo_root),
                "name": repo_name,
                "status": "up_to_date",
                "base_ref": base_ref,
                "used_agcoord": use_agc,
                "gate": profile.gate,
                "changed_files": [],
                "upgrade_actions": upgrade_actions,
                "log_path": None,
                "log_excerpt": None,
                "issue_url": None,
                "issue_number": None,
                "issue_created": None,
                "pr_url": None,
                "pr_number": None,
                "land_method": None,
                "error": None,
            }

        if not land:
            return {
                "path": str(repo_root),
                "name": repo_name,
                "status": "changes_verified",
                "base_ref": base_ref,
                "used_agcoord": use_agc,
                "gate": profile.gate,
                "changed_files": changed_files,
                "upgrade_actions": upgrade_actions,
                "log_path": None,
                "log_excerpt": None,
                "issue_url": None,
                "issue_number": None,
                "issue_created": None,
                "pr_url": None,
                "pr_number": None,
                "land_method": None,
                "error": None,
            }

        # --land path: branch, commit, push, tracking issue, PR, and land/merge.
        if not has_origin:
            return _error_result(
                str(repo_root),
                "Cannot --land verified changes without an 'origin' remote.",
                base_ref=base_ref,
                used_agcoord=use_agc,
                gate=profile.gate,
                changed_files=changed_files,
                upgrade_actions=upgrade_actions,
            )

        gh_bin = shutil.which("gh", path=env.get("PATH"))
        if gh_bin is None:
            return _error_result(
                str(repo_root),
                "Cannot --land verified changes because 'gh' CLI was not found on PATH.",
                base_ref=base_ref,
                used_agcoord=use_agc,
                gate=profile.gate,
                changed_files=changed_files,
                upgrade_actions=upgrade_actions,
            )

        # Reuse the pre-gate candidate commit; commit now only if the gate was skipped.
        if created_branch is None:
            created_branch, commit_err = _commit_candidate_branch(
                wt_dir,
                repo_root=repo_root,
                stamp=stamp,
                changed_files=changed_files,
                commit_title=commit_title,
                env=env,
            )
            if commit_err or created_branch is None:
                return _error_result(
                    str(repo_root),
                    commit_err or "Failed to create the candidate branch.",
                    base_ref=base_ref,
                    used_agcoord=use_agc,
                    gate=profile.gate,
                    changed_files=changed_files,
                    upgrade_actions=upgrade_actions,
                )
        branch_name = created_branch

        push_res = run_cmd(
            ["git", "push", "-u", "origin", branch_name],
            cwd=wt_dir,
            timeout=UPGRADE_CMD_TIMEOUT_SECONDS,
            env=env,
        )
        if not push_res.ok:
            detail = push_res.stderr.strip() or push_res.stdout.strip()
            return _error_result(
                str(repo_root),
                f"git push -u origin {branch_name} failed: {detail}",
                base_ref=base_ref,
                used_agcoord=use_agc,
                gate=profile.gate,
                changed_files=changed_files,
                upgrade_actions=upgrade_actions,
            )

        files_md = "\n".join(f"- `{f}`" for f in changed_files)
        issue_body = f"Automated `gardenia repos upgrade` verified updates for `{repo_name}`:\n\n{files_md}\n"
        issue_res = run_cmd(
            [gh_bin, "issue", "create", "--title", commit_title, "--body", issue_body],
            cwd=wt_dir,
            timeout=30.0,
            env=env,
        )
        if not issue_res.ok:
            detail = issue_res.stderr.strip() or issue_res.stdout.strip()
            return _error_result(
                str(repo_root),
                f"gh issue create failed: {detail}",
                base_ref=base_ref,
                used_agcoord=use_agc,
                gate=profile.gate,
                changed_files=changed_files,
                upgrade_actions=upgrade_actions,
            )
        issue_url = _extract_url(issue_res.stdout)
        issue_number = _extract_trailing_number(issue_url)

        closes_line = f"Closes #{issue_number}\n\n" if issue_number is not None else ""
        pr_body = f"{closes_line}Automated `gardenia repos upgrade` sweep.\n\nChanged files:\n{files_md}\n"
        pr_cmd = [gh_bin, "pr", "create", "--title", commit_title, "--body", pr_body, "--head", branch_name]
        if base_ref.startswith("origin/"):
            pr_cmd.extend(["--base", base_ref.split("/", 1)[1]])
        pr_res = run_cmd(pr_cmd, cwd=wt_dir, timeout=30.0, env=env)
        if not pr_res.ok:
            detail = pr_res.stderr.strip() or pr_res.stdout.strip()
            return _error_result(
                str(repo_root),
                f"gh pr create failed: {detail}",
                base_ref=base_ref,
                used_agcoord=use_agc,
                gate=profile.gate,
                changed_files=changed_files,
                upgrade_actions=upgrade_actions,
                issue_url=issue_url,
                issue_number=issue_number,
                issue_created=True,
            )
        pr_url = _extract_url(pr_res.stdout)
        pr_number = _extract_trailing_number(pr_url)
        pr_ref = str(pr_number) if pr_number is not None else (pr_url or branch_name)

        is_docs_only = profile.docs_exemption and qualifies_for_docs_exemption(changed_files)
        if is_docs_only or not use_agc:
            land_method = "gh_pr_merge_docs_exemption" if is_docs_only else "gh_pr_merge"
            land_cmd = [gh_bin, "pr", "merge", pr_ref, "--merge", "--delete-branch"]
            land_res = run_cmd(land_cmd, cwd=wt_dir, timeout=UPGRADE_CMD_TIMEOUT_SECONDS, env=env)
        else:
            land_method = "agc_land"
            land_label = (
                f"land #{pr_number} upgrade sweep ({repo_name})"
                if pr_number is not None
                else f"land upgrade sweep ({repo_name})"
            )
            land_cmd = [
                "agc",
                "land",
                pr_ref,
                "--label",
                land_label,
                *_build_agc_resource_args(profile),
                "--",
                "bash",
                "-lc",
                profile.gate,
            ]
            land_res = run_cmd(land_cmd, cwd=wt_dir, timeout=gate_timeout, env=gate_env)

        if not land_res.ok:
            detail = land_res.stderr.strip() or land_res.stdout.strip() or f"exit code {land_res.returncode}"
            return _error_result(
                str(repo_root),
                f"Failed to land PR {pr_ref} via {land_method}: {detail}",
                base_ref=base_ref,
                used_agcoord=use_agc,
                gate=profile.gate,
                changed_files=changed_files,
                upgrade_actions=upgrade_actions,
                issue_url=issue_url,
                issue_number=issue_number,
                issue_created=True,
                pr_url=pr_url,
                pr_number=pr_number,
                land_method=land_method,
            )

        return {
            "path": str(repo_root),
            "name": repo_name,
            "status": "landed",
            "base_ref": base_ref,
            "used_agcoord": use_agc,
            "gate": profile.gate,
            "changed_files": changed_files,
            "upgrade_actions": upgrade_actions,
            "log_path": None,
            "log_excerpt": None,
            "issue_url": issue_url,
            "issue_number": issue_number,
            "issue_created": True,
            "pr_url": pr_url,
            "pr_number": pr_number,
            "land_method": land_method,
            "branch": branch_name,
            "error": None,
        }
    finally:
        if worktree_created:
            run_cmd(
                ["git", "worktree", "remove", "--force", str(wt_dir)],
                cwd=repo_root,
                timeout=30.0,
                env=env,
            )
        if created_branch is not None:
            run_cmd(
                ["git", "branch", "-D", created_branch],
                cwd=repo_root,
                timeout=DEFAULT_TIMEOUT_SECONDS,
                env=env,
            )
        shutil.rmtree(scratch_dir, ignore_errors=True)


def upgrade_repos(
    *,
    repos: Sequence[str | os.PathLike[str]] | None = None,
    root: str | os.PathLike[str] | None = None,
    max_depth: int = 2,
    packages: Sequence[str] | None = None,
    sync_policy: bool = True,
    upgrade_locks: bool = True,
    land: bool = False,
    file_issues: bool = False,
    gate_timeout: float = GATE_TIMEOUT_SECONDS,
    worktree_root: str | os.PathLike[str] | None = None,
    log_dir: str | os.PathLike[str] | None = None,
    dry_run: bool = False,
    env: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Run an isolated-worktree policy and dependency upgrade sweep across target repositories."""

    if not is_finite_number(gate_timeout) or float(gate_timeout) <= 0:
        raise GardeniaError("--gate-timeout must be a finite number > 0.", exit_code=2)
    effective_gate_timeout = float(gate_timeout)

    validated_packages = validate_packages(packages)
    effective_env = _effective_env(env)
    targets = resolve_fleet_targets(repos=repos, root=root, max_depth=max_depth, env=effective_env)

    resolved_wt_root = effective_worktree_root(worktree_root, effective_env)
    resolved_log_dir = effective_log_dir(log_dir, effective_env)

    results: list[dict[str, Any]] = []
    for target in targets:
        if dry_run:
            results.append(
                _plan_single_repo_upgrade(
                    target,
                    packages=validated_packages,
                    sync_policy=sync_policy,
                    upgrade_locks=upgrade_locks,
                    land=land,
                    env=effective_env,
                )
            )
        else:
            results.append(
                _execute_single_repo_upgrade(
                    target,
                    packages=validated_packages,
                    sync_policy=sync_policy,
                    upgrade_locks=upgrade_locks,
                    land=land,
                    file_issues=file_issues,
                    gate_timeout=effective_gate_timeout,
                    wt_root=resolved_wt_root,
                    resolved_log_dir=resolved_log_dir,
                    env=effective_env,
                )
            )

    summary = {
        "total": len(results),
        "planned": sum(1 for r in results if r["status"] == "planned"),
        "up_to_date": sum(1 for r in results if r["status"] == "up_to_date"),
        "changes_verified": sum(1 for r in results if r["status"] == "changes_verified"),
        "landed": sum(1 for r in results if r["status"] == "landed"),
        "gate_failed": sum(1 for r in results if r["status"] == "gate_failed"),
        "error": sum(1 for r in results if r["status"] == "error"),
    }
    ok = (summary["gate_failed"] == 0) and (summary["error"] == 0)
    return {
        "format": REPOS_UPGRADE_FORMAT,
        "version": __version__,
        "ok": ok,
        "dry_run": dry_run,
        "options": {
            "packages": validated_packages,
            "sync_policy": sync_policy,
            "upgrade_locks": upgrade_locks,
            "land": land,
            "file_issues": file_issues,
            "gate_timeout": effective_gate_timeout,
            "worktree_root": str(resolved_wt_root),
            "log_dir": str(resolved_log_dir),
        },
        "repos": results,
        "summary": summary,
    }
