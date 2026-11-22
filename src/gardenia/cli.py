"""Command-line interface for gardenia."""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any, Sequence

from gardenia import __version__
from gardenia.host import check_host, parse_clients_arg, setup_host
from gardenia.models import CHECK_FORMAT, Finding, GardeniaError, compute_ok
from gardenia.repo import check_repo, init_repo, render_repo, try_resolve_git_repo
from gardenia.repos import (
    add_repos,
    check_repos,
    list_repos,
    prune_repos,
    remove_repos,
    scan_repos,
    upgrade_repos,
)


def _add_profile_flags(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--repo", default=None, help="Target Git repository path (default: current Git top-level).")
    parser.add_argument("--gate", default=None, help="Repository validation command (default: 'bash scripts/check').")
    parser.add_argument("--cpu", type=int, default=None, help="CPU resource claim for agcoord (default: 4).")
    parser.add_argument("--memory-gb", type=float, default=None, help="Memory resource claim in GiB (default: 4.0).")
    parser.add_argument("--tmpfs-gb", type=float, default=None, help="Optional tmpfs resource claim in GiB.")

    overflow = parser.add_mutually_exclusive_group()
    overflow.add_argument(
        "--no-overflow",
        dest="no_overflow",
        action="store_true",
        default=None,
        help="Include --no-overflow in agcoord check/land commands (default).",
    )
    overflow.add_argument(
        "--allow-overflow",
        dest="no_overflow",
        action="store_false",
        default=None,
        help="Omit --no-overflow from agcoord check/land commands.",
    )

    parser.add_argument(
        "--routing",
        choices=("agroute", "howhard", "none"),
        default=None,
        help="Worker delegation routing layer (default: agroute).",
    )
    parser.add_argument(
        "--agcoord",
        choices=("auto", "required", "none"),
        default=None,
        help="AGCoord coordination policy (default: auto).",
    )

    docs_ex = parser.add_mutually_exclusive_group()
    docs_ex.add_argument(
        "--docs-exemption",
        "--docs-only-exemption",
        dest="docs_exemption",
        action="store_true",
        default=None,
        help="Include the documentation-only exemption clause (default).",
    )
    docs_ex.add_argument(
        "--no-docs-exemption",
        "--no-docs-only-exemption",
        dest="docs_exemption",
        action="store_false",
        default=None,
        help="Omit the documentation-only exemption clause.",
    )

    claude_sym = parser.add_mutually_exclusive_group()
    claude_sym.add_argument(
        "--claude-symlink",
        dest="claude_symlink",
        action="store_true",
        default=None,
        help="Manage CLAUDE.md -> AGENTS.md symlink (default).",
    )
    claude_sym.add_argument(
        "--no-claude-symlink",
        dest="claude_symlink",
        action="store_false",
        default=None,
        help="Do not create or require CLAUDE.md -> AGENTS.md symlink.",
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="gardenia",
        description=(
            "Bootstrap, initialize, and verify the coding-agent stack (agrein, agcoord, howhard, agroute) "
            "with explicit per-host vs. per-repository separation."
        ),
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"gardenia {__version__}",
    )

    subparsers = parser.add_subparsers(dest="command", required=True)

    # Top-level unified check
    check_parser = subparsers.add_parser(
        "check",
        help="Run host check (and repo check when inside a Git repository or --repo is supplied).",
    )
    check_parser.add_argument("--repo", default=None, help="Target Git repository path.")
    check_parser.add_argument("--clients", default=None, help="Comma-separated agent clients (codex,claude,agy).")
    check_parser.add_argument(
        "--with-agcoord",
        action="store_true",
        default=False,
        help="Check agcoord even on non-Linux platforms.",
    )
    check_parser.add_argument("--strict", action="store_true", default=False, help="Treat warnings as failures.")
    check_parser.add_argument("--json", action="store_true", default=False, help="Emit machine-readable JSON.")

    # Per-host subcommands
    host_parser = subparsers.add_parser(
        "host",
        help="Inspect or configure per-host workstation tools, broker, plugins, and reserve config.",
    )
    host_sub = host_parser.add_subparsers(dest="host_command", required=True)

    host_check = host_sub.add_parser("check", help="Read-only inspection of per-host tools, broker, plugins, and reserves.")
    host_check.add_argument("--clients", default=None, help="Comma-separated agent clients (codex,claude,agy).")
    host_check.add_argument(
        "--with-agcoord",
        action="store_true",
        default=False,
        help="Check agcoord even on non-Linux platforms.",
    )
    host_check.add_argument("--strict", action="store_true", default=False, help="Treat warnings as failures.")
    host_check.add_argument("--json", action="store_true", default=False, help="Emit machine-readable JSON.")

    host_setup = host_sub.add_parser("setup", help="Install or upgrade host CLIs, broker, user plugins, and reserve config.")
    host_setup.add_argument("--clients", default=None, help="Comma-separated agent clients (codex,claude,agy).")
    host_setup.add_argument(
        "--installer",
        choices=("auto", "uv", "pipx"),
        default="auto",
        help="Python CLI installer backend (default: auto).",
    )
    host_setup.add_argument(
        "--broker",
        choices=("auto", "user", "enforced", "skip"),
        default="auto",
        help="AGCoord host broker strategy on Linux (default: auto).",
    )
    host_setup.add_argument(
        "--force-broker",
        action="store_true",
        default=False,
        help="Allow --broker user over an active cgroup-v2 broker or override broker probe timeout refusal.",
    )
    host_setup.add_argument("--upgrade", action="store_true", default=False, help="Upgrade installed CLIs and plugins.")
    host_setup.add_argument("--reserve-pct", type=float, default=None, help="Write host_reserve_pct to ~/.config/agroute/config.toml.")
    host_setup.add_argument(
        "--host-reserve-credit",
        type=float,
        default=None,
        help="Write host_reserve_credit_usd to ~/.config/agroute/config.toml.",
    )
    host_setup.add_argument(
        "--min-remaining-credit",
        type=float,
        default=None,
        help="Write min_remaining_credit_usd to ~/.config/agroute/config.toml.",
    )
    host_setup.add_argument(
        "--overwrite-config",
        action="store_true",
        default=False,
        help="Allow overwriting an existing ~/.config/agroute/config.toml.",
    )
    host_setup.add_argument(
        "--with-agcoord",
        action="store_true",
        default=False,
        help="Include agcoord even on non-Linux platforms.",
    )
    host_setup.add_argument("--dry-run", action="store_true", default=False, help="Plan actions without mutating the host.")
    host_setup.add_argument("--json", action="store_true", default=False, help="Emit machine-readable JSON.")

    # Per-repo subcommands
    repo_parser = subparsers.add_parser(
        "repo",
        help="Inspect, render, or initialize per-repository policy and inspect local .git/ state.",
    )
    repo_sub = repo_parser.add_subparsers(dest="repo_command", required=True)

    repo_check = repo_sub.add_parser("check", help="Read-only inspection of tracked repo settings and local .git/ state.")
    repo_check.add_argument("--repo", default=None, help="Target Git repository path (default: current Git top-level).")
    repo_check.add_argument("--strict", action="store_true", default=False, help="Treat warnings as failures.")
    repo_check.add_argument("--json", action="store_true", default=False, help="Emit machine-readable JSON.")

    repo_init = repo_sub.add_parser("init", help="Insert/update managed AGENTS.md block, CLAUDE.md symlink, and optional gardenia.toml.")
    _add_profile_flags(repo_init)
    repo_init.add_argument(
        "--force-claude-symlink",
        action="store_true",
        default=False,
        help="Replace an existing non-matching CLAUDE.md with a symlink to AGENTS.md.",
    )
    repo_init.add_argument(
        "--save-config",
        action="store_true",
        default=False,
        help="Persist resolved profile settings to gardenia.toml.",
    )
    repo_init.add_argument(
        "--allow-worktree",
        action="store_true",
        default=False,
        help="Allow running repo init inside a linked Git worktree.",
    )
    reg_group = repo_init.add_mutually_exclusive_group()
    reg_group.add_argument(
        "--register",
        dest="register",
        action="store_true",
        default=None,
        help="Register the repository in the host repos registry (default for primary checkouts; refused on linked worktrees).",
    )
    reg_group.add_argument(
        "--no-register",
        dest="register",
        action="store_false",
        default=None,
        help="Do not register the repository in the host repos registry.",
    )
    repo_init.add_argument("--dry-run", action="store_true", default=False, help="Plan edits without writing files.")
    repo_init.add_argument("--json", action="store_true", default=False, help="Emit machine-readable JSON.")

    repo_render = repo_sub.add_parser("render", help="Print the rendered managed AGENTS.md block without modifying files.")
    _add_profile_flags(repo_render)
    repo_render.add_argument("--json", action="store_true", default=False, help="Emit machine-readable JSON.")

    # Multi-repository fleet subcommands
    repos_parser = subparsers.add_parser(
        "repos",
        help="Manage the host repository registry and run fleet checks or isolated-worktree upgrade sweeps.",
    )
    repos_sub = repos_parser.add_subparsers(dest="repos_command", required=True)

    repos_list = repos_sub.add_parser("list", help="List registered (and optionally discovered) managed repositories.")
    repos_list.add_argument("--root", default=None, help="Optional workspace root to scan read-only for additional managed repos.")
    repos_list.add_argument("--max-depth", type=int, default=2, help="Maximum directory depth when scanning --root (default: 2).")
    repos_list.add_argument("--strict", action="store_true", default=False, help="Exit 1 if any listed repo is missing, non-primary, unmanaged, or out of sync.")
    repos_list.add_argument("--json", action="store_true", default=False, help="Emit gardenia-repos-list-v1 JSON.")

    repos_add = repos_sub.add_parser("add", help="Register one or more primary Git repositories in repos.toml.")
    repos_add.add_argument("paths", nargs="*", metavar="PATH", help="Repository path(s) to register (default: current directory).")
    repos_add.add_argument("--json", action="store_true", default=False, help="Emit machine-readable JSON.")

    repos_remove = repos_sub.add_parser("remove", help="Remove repository path(s) from repos.toml idempotently.")
    repos_remove.add_argument("paths", nargs="+", metavar="PATH", help="Repository path(s) to remove.")
    repos_remove.add_argument("--json", action="store_true", default=False, help="Emit machine-readable JSON.")

    repos_scan = repos_sub.add_parser("scan", help="Discover primary gardenia-managed Git repositories under --root and register them.")
    repos_scan.add_argument("--root", required=True, help="Root directory to walk for managed Git repositories.")
    repos_scan.add_argument("--max-depth", type=int, default=2, help="Maximum directory depth under --root (default: 2).")
    repos_scan.add_argument("--dry-run", action="store_true", default=False, help="Discover repositories without modifying repos.toml.")
    repos_scan.add_argument("--json", action="store_true", default=False, help="Emit machine-readable JSON.")

    repos_prune = repos_sub.add_parser("prune", help="Remove stale, non-Git, or linked-worktree entries from repos.toml.")
    repos_prune.add_argument("--dry-run", action="store_true", default=False, help="Report entries that would be removed without modifying repos.toml.")
    repos_prune.add_argument("--json", action="store_true", default=False, help="Emit machine-readable JSON.")

    repos_check = repos_sub.add_parser("check", help="Run read-only gardenia repo check across all target repositories.")
    repos_check.add_argument("--root", default=None, help="Optional workspace root to include in discovery.")
    repos_check.add_argument("--repo", dest="repos", action="append", default=None, help="Explicit target repository path (repeatable; overrides registry).")
    repos_check.add_argument("--max-depth", type=int, default=2, help="Maximum directory depth when scanning --root (default: 2).")
    repos_check.add_argument("--strict", action="store_true", default=False, help="Treat warnings as failures.")
    repos_check.add_argument("--json", action="store_true", default=False, help="Emit gardenia-repos-check-v1 JSON.")

    repos_upgrade = repos_sub.add_parser(
        "upgrade",
        help="Run isolated-worktree policy sync, lock/dependency upgrades, and validation gates across target repositories.",
    )
    repos_upgrade.add_argument("--root", default=None, help="Optional workspace root to include in discovery.")
    repos_upgrade.add_argument("--repo", dest="repos", action="append", default=None, help="Explicit target repository path (repeatable; overrides registry).")
    repos_upgrade.add_argument("--max-depth", type=int, default=2, help="Maximum directory depth when scanning --root (default: 2).")
    repos_upgrade.add_argument("--package", dest="packages", action="append", default=None, help="Specific dependency package name to upgrade (repeatable).")

    sync_group = repos_upgrade.add_mutually_exclusive_group()
    sync_group.add_argument(
        "--sync-policy",
        dest="sync_policy",
        action="store_true",
        default=True,
        help="Refresh AGENTS.md/CLAUDE.md via gardenia repo init inside the isolated worktree (default).",
    )
    sync_group.add_argument(
        "--no-sync-policy",
        dest="sync_policy",
        action="store_false",
        help="Skip AGENTS.md/CLAUDE.md synchronization.",
    )

    locks_group = repos_upgrade.add_mutually_exclusive_group()
    locks_group.add_argument(
        "--upgrade-locks",
        dest="upgrade_locks",
        action="store_true",
        default=True,
        help="Refresh uv.lock/Cargo.lock or provision a fresh test venv for pyproject.toml repos (default).",
    )
    locks_group.add_argument(
        "--no-upgrade-locks",
        dest="upgrade_locks",
        action="store_false",
        help="Skip lockfile and dependency upgrades.",
    )

    repos_upgrade.add_argument(
        "--land",
        action="store_true",
        default=False,
        help="Commit verified updates on an upgrade branch, open tracking issue + PR, and land via agc land (or gh pr merge when docs-only).",
    )
    repos_upgrade.add_argument(
        "--file-issues",
        action="store_true",
        default=False,
        help="Open (or reuse an existing open) GitHub issue when a repository gate fails.",
    )
    repos_upgrade.add_argument("--worktree-root", default=None, help="Base directory for temporary isolated worktrees (default: /var/tmp or system temp dir).")
    repos_upgrade.add_argument("--log-dir", default=None, help="Directory for persisted failure logs (default: $GARDENIA_LOG_DIR or ~/.local/state/gardenia/logs).")
    repos_upgrade.add_argument("--dry-run", action="store_true", default=False, help="Inspect targets and print planned steps without creating worktrees or running gates.")
    repos_upgrade.add_argument("--json", action="store_true", default=False, help="Emit gardenia-repos-upgrade-v1 JSON.")

    return parser


def _format_findings_human(findings: list[dict[str, Any]]) -> list[str]:
    if not findings:
        return ["Findings: none"]
    lines = ["Findings:"]
    for item in findings:
        lines.append(f"  [{item['scope']}][{item['severity']}] {item['component']}: {item['message']}")
        if item.get("remediation"):
            lines.append(f"    remediation: {item['remediation']}")
    return lines


def _format_host_human(host: dict[str, Any]) -> list[str]:
    lines = [f"Host (workstation: {host['platform']}):"]
    for pkg, info in host["clis"].items():
        ver = info["version"] or "-"
        lines.append(f"  CLI {info['binary']} ({pkg}): {info['status']} ({ver})")
    broker = host["broker"]
    broker_detail = broker["mode"] or broker.get("reason") or broker["status"]
    lines.append(f"  AGCoord broker: {broker['status']} ({broker_detail})")
    if host["clients"]:
        for client, cinfo in host["clients"].items():
            if not cinfo["installed"]:
                lines.append(f"  Client {client}: not installed")
                continue
            parts = []
            for pkg, pinfo in cinfo.get("plugins", {}).items():
                if not pinfo.get("installed"):
                    parts.append(f"{pkg}=missing")
                elif not pinfo.get("enabled"):
                    parts.append(f"{pkg}=disabled")
                else:
                    parts.append(f"{pkg}={pinfo.get('version') or 'ok'}")
            lines.append(f"  Client {client}: {', '.join(parts) if parts else 'error'}")
    else:
        lines.append("  Clients: none detected")
    cfg = host["agroute_config"]
    if not cfg["exists"]:
        lines.append(f"  Host agroute config: not present ({cfg['path']})")
    elif cfg["valid"]:
        agents_str = ", ".join(sorted(cfg["agents"].keys())) or "none"
        lines.append(f"  Host agroute config: valid ({cfg['path']}; agents: {agents_str})")
    else:
        lines.append(f"  Host agroute config: invalid ({cfg['path']})")
    return lines


def _format_repo_human(repo: dict[str, Any]) -> list[str]:
    tracked = repo["tracked"]
    local = repo["local_state"]
    lines = [f"Repository ({tracked['root']}):"]
    wt_label = "linked worktree" if tracked["is_linked_worktree"] else "primary checkout"
    lines.append(f"  Tracked settings ({wt_label}):")
    cfg = tracked["config"]
    lines.append(f"    config: {cfg['source']} ({'valid' if cfg['valid'] else 'invalid'})")
    amd = tracked["agents_md"]
    if not amd["exists"]:
        amd_status = "missing"
    elif amd["block_malformed"]:
        amd_status = "malformed markers"
    elif not amd["has_managed_block"]:
        amd_status = "present (no managed block)"
    elif amd["in_sync"]:
        amd_status = "in sync"
    else:
        amd_status = "out of sync"
    lines.append(f"    AGENTS.md: {amd_status}")
    lines.append(f"    CLAUDE.md: {tracked['claude_md']['status']}")
    lines.append(f"  Local per-clone .git/ state ({local['git_common_dir']}):")
    hh = local["howhard_store"]
    hh_note = "present" if hh["exists"] else "not initialized"
    if hh["has_legacy_loose_json"]:
        hh_note += f" (legacy loose JSON: {hh['legacy_requests_json_count']} requests, {hh['legacy_decisions_json_count']} decisions)"
    lines.append(f"    howhard store: {hh_note}")
    ar = local["agroute_store"]
    ar_note = "present" if ar["exists"] else "not initialized"
    if ar["prices_pending_checked"]:
        ar_note += f" (pending prices: {ar['pending_price_count']})"
    lines.append(f"    agroute store: {ar_note}")
    return lines


def _format_actions_human(title: str, actions: list[dict[str, Any]], *, dry_run: bool) -> list[str]:
    prefix = "[dry-run] " if dry_run else ""
    lines = [f"{prefix}{title}:"]
    for action in actions:
        status = action["status"]
        line = f"  [{status}] {action['component']}: {action['description']}"
        if action.get("command"):
            line += f"\n    $ {' '.join(action['command'])}"
        elif action.get("path"):
            line += f" ({action['path']})"
        lines.append(line)
    return lines


def _format_repos_list_human(res: dict[str, Any]) -> list[str]:
    lines = [f"Host repository registry ({res['registry_path']}):"]
    if not res["repos"]:
        lines.append("  (no repositories)")
        return lines
    for item in res["repos"]:
        tags: list[str] = []
        tags.append("registered" if item["registered"] else "discovered")
        if not item["exists"]:
            tags.append("missing")
        elif not item["is_git_repo"]:
            tags.append("not-git")
        else:
            if item["is_linked_worktree"]:
                tags.append("linked-worktree")
            tags.append(f"branch={item['branch']}")
            tags.append("clean" if item["clean"] else "dirty")
            tags.append("managed" if item["managed"] else "unmanaged")
            if item["agents_md_in_sync"] is True:
                tags.append("agents=in-sync")
            elif item["agents_md_in_sync"] is False:
                tags.append("agents=out-of-sync")
        lines.append(f"  {item['name']} ({item['path']}): {', '.join(tags)}")
    summary = res["summary"]
    lines.append(
        f"Summary: total={summary['total']}, registered={summary['registered']}, "
        f"managed={summary['managed']}, in_sync={summary['in_sync']}, issues={summary['issues']}"
    )
    return lines


def _format_repos_check_human(res: dict[str, Any]) -> list[str]:
    lines = [f"Fleet check ({res['summary']['total']} repositories):"]
    for item in res["repos"]:
        status = "ok" if item["ok"] else "fail"
        counts = item["counts"]
        lines.append(
            f"  [{status}] {item['name']} ({item['path']}): "
            f"{counts['errors']} error(s), {counts['warnings']} warning(s)"
        )
        for finding in item["findings"]:
            if finding["severity"] in ("error", "warning"):
                lines.append(f"    [{finding['severity']}] {finding['component']}: {finding['message']}")
    summary = res["summary"]
    lines.append(
        f"Summary: ok={summary['ok']}/{summary['total']}, failed={summary['failed']}, "
        f"errors={summary['errors']}, warnings={summary['warnings']}"
    )
    return lines


def _format_repos_upgrade_human(res: dict[str, Any]) -> list[str]:
    prefix = "[dry-run] " if res["dry_run"] else ""
    lines = [f"{prefix}Fleet upgrade ({res['summary']['total']} repositories):"]
    for item in res["repos"]:
        detail_parts: list[str] = []
        if item.get("changed_files"):
            detail_parts.append(f"changed={len(item['changed_files'])}")
        if item.get("pr_url"):
            detail_parts.append(f"pr={item['pr_url']}")
        if item.get("issue_url"):
            detail_parts.append(f"issue={item['issue_url']}")
        if item.get("log_path"):
            detail_parts.append(f"log={item['log_path']}")
        if item.get("planned_steps"):
            detail_parts.append(f"steps={','.join(item['planned_steps'])}")
        if item.get("error"):
            detail_parts.append(f"error={item['error']}")
        suffix = f" ({'; '.join(detail_parts)})" if detail_parts else ""
        lines.append(f"  [{item['status']}] {item['name']} ({item['path']}){suffix}")
    s = res["summary"]
    lines.append(
        f"Summary: total={s['total']}, up_to_date={s['up_to_date']}, "
        f"changes_verified={s['changes_verified']}, landed={s['landed']}, "
        f"gate_failed={s['gate_failed']}, error={s['error']}"
    )
    return lines


def _emit_json(payload: dict[str, Any]) -> None:
    clean = {k: v for k, v in payload.items() if not k.startswith("_")}
    print(json.dumps(clean, indent=2))


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        return int(exc.code) if isinstance(exc.code, int) else 2

    want_json = bool(getattr(args, "json", False))

    try:
        if args.command == "check":
            clients = parse_clients_arg(args.clients)
            host_res = check_host(
                clients=clients,
                with_agcoord=args.with_agcoord,
                strict=args.strict,
            )
            all_findings: list[Finding] = list(host_res["_findings"])
            repo_payload: dict[str, Any] | None = None
            if args.repo is not None:
                repo_res = check_repo(args.repo, strict=args.strict)
                repo_payload = repo_res["repo"]
                all_findings.extend(repo_res["_findings"])
            else:
                if try_resolve_git_repo(None) is not None:
                    repo_res = check_repo(None, strict=args.strict)
                    repo_payload = repo_res["repo"]
                    all_findings.extend(repo_res["_findings"])

            ok = compute_ok(all_findings, strict=args.strict)
            payload = {
                "format": CHECK_FORMAT,
                "version": __version__,
                "ok": ok,
                "strict": args.strict,
                "host": host_res["host"],
                "repo": repo_payload,
                "findings": [f.to_dict() for f in all_findings],
            }
            if want_json:
                _emit_json(payload)
            else:
                lines = _format_host_human(payload["host"])
                if repo_payload is not None:
                    lines.append("")
                    lines.extend(_format_repo_human(repo_payload))
                else:
                    lines.append("")
                    lines.append("Repository: skipped (current directory is not inside a Git repository)")
                lines.append("")
                lines.extend(_format_findings_human(payload["findings"]))
                print("\n".join(lines))
            return 0 if ok else 1

        if args.command == "host":
            clients = parse_clients_arg(args.clients)
            if args.host_command == "check":
                res = check_host(
                    clients=clients,
                    with_agcoord=args.with_agcoord,
                    strict=args.strict,
                )
                if want_json:
                    _emit_json(res)
                else:
                    lines = _format_host_human(res["host"])
                    lines.append("")
                    lines.extend(_format_findings_human(res["findings"]))
                    print("\n".join(lines))
                return 0 if res["ok"] else 1

            if args.host_command == "setup":
                res = setup_host(
                    clients=clients,
                    installer=args.installer,
                    broker=args.broker,
                    force_broker=args.force_broker,
                    upgrade=args.upgrade,
                    reserve_pct=args.reserve_pct,
                    host_reserve_credit=args.host_reserve_credit,
                    min_remaining_credit=args.min_remaining_credit,
                    overwrite_config=args.overwrite_config,
                    with_agcoord=args.with_agcoord,
                    dry_run=args.dry_run,
                )
                if want_json:
                    _emit_json(res)
                else:
                    lines = _format_actions_human("Host setup", res["actions"], dry_run=args.dry_run)
                    print("\n".join(lines))
                return 0 if res["ok"] else 1

        if args.command == "repo":
            if args.repo_command == "check":
                res = check_repo(args.repo, strict=args.strict)
                if want_json:
                    _emit_json(res)
                else:
                    lines = _format_repo_human(res["repo"])
                    lines.append("")
                    lines.extend(_format_findings_human(res["findings"]))
                    print("\n".join(lines))
                return 0 if res["ok"] else 1

            if args.repo_command == "render":
                res = render_repo(
                    repo_path=args.repo,
                    gate=args.gate,
                    cpu=args.cpu,
                    memory_gb=args.memory_gb,
                    tmpfs_gb=args.tmpfs_gb,
                    no_overflow=args.no_overflow,
                    routing=args.routing,
                    agcoord=args.agcoord,
                    docs_exemption=args.docs_exemption,
                    claude_symlink=args.claude_symlink,
                )
                if want_json:
                    _emit_json(res)
                else:
                    print(res["managed_block"])
                return 0

            if args.repo_command == "init":
                res = init_repo(
                    repo_path=args.repo,
                    gate=args.gate,
                    cpu=args.cpu,
                    memory_gb=args.memory_gb,
                    tmpfs_gb=args.tmpfs_gb,
                    no_overflow=args.no_overflow,
                    routing=args.routing,
                    agcoord=args.agcoord,
                    docs_exemption=args.docs_exemption,
                    claude_symlink=args.claude_symlink,
                    force_claude_symlink=args.force_claude_symlink,
                    save_config=args.save_config,
                    allow_worktree=args.allow_worktree,
                    register=args.register,
                    dry_run=args.dry_run,
                )
                if want_json:
                    _emit_json(res)
                else:
                    lines = _format_actions_human(f"Repository init ({res['root']})", res["actions"], dry_run=args.dry_run)
                    print("\n".join(lines))
                return 0 if res["ok"] else 1

        if args.command == "repos":
            if args.repos_command == "list":
                res = list_repos(root=args.root, max_depth=args.max_depth, strict=args.strict)
                if want_json:
                    _emit_json(res)
                else:
                    print("\n".join(_format_repos_list_human(res)))
                return 0 if res["ok"] else 1

            if args.repos_command == "add":
                res = add_repos(args.paths)
                if want_json:
                    _emit_json(res)
                else:
                    print(
                        f"Registry ({res['registry_path']}): added {len(res['added'])}, "
                        f"already registered {len(res['already_registered'])}, total {len(res['repos'])}."
                    )
                return 0

            if args.repos_command == "remove":
                res = remove_repos(args.paths)
                if want_json:
                    _emit_json(res)
                else:
                    print(
                        f"Registry ({res['registry_path']}): removed {len(res['removed'])}, "
                        f"not found {len(res['not_found'])}, total {len(res['repos'])}."
                    )
                return 0

            if args.repos_command == "scan":
                res = scan_repos(root=args.root, max_depth=args.max_depth, dry_run=args.dry_run)
                if want_json:
                    _emit_json(res)
                else:
                    prefix = "[dry-run] " if args.dry_run else ""
                    print(
                        f"{prefix}Scan ({res['root']}): discovered {len(res['discovered'])}, "
                        f"added {len(res['added'])}, already registered {len(res['already_registered'])}, "
                        f"total {len(res['repos'])}."
                    )
                return 0

            if args.repos_command == "prune":
                res = prune_repos(dry_run=args.dry_run)
                if want_json:
                    _emit_json(res)
                else:
                    prefix = "[dry-run] " if args.dry_run else ""
                    print(
                        f"{prefix}Prune ({res['registry_path']}): removed {len(res['removed'])}, "
                        f"kept {len(res['kept'])}."
                    )
                return 0

            if args.repos_command == "check":
                res = check_repos(
                    repos=args.repos,
                    root=args.root,
                    max_depth=args.max_depth,
                    strict=args.strict,
                )
                if want_json:
                    _emit_json(res)
                else:
                    print("\n".join(_format_repos_check_human(res)))
                return 0 if res["ok"] else 1

            if args.repos_command == "upgrade":
                res = upgrade_repos(
                    repos=args.repos,
                    root=args.root,
                    max_depth=args.max_depth,
                    packages=args.packages,
                    sync_policy=args.sync_policy,
                    upgrade_locks=args.upgrade_locks,
                    land=args.land,
                    file_issues=args.file_issues,
                    worktree_root=args.worktree_root,
                    log_dir=args.log_dir,
                    dry_run=args.dry_run,
                )
                if want_json:
                    _emit_json(res)
                else:
                    print("\n".join(_format_repos_upgrade_human(res)))
                return 0 if res["ok"] else 1

    except GardeniaError as exc:
        if want_json:
            print(json.dumps({"ok": False, "error": exc.message, "exit_code": exc.exit_code}, indent=2))
        else:
            print(f"error: {exc.message}", file=sys.stderr)
        return exc.exit_code

    return 2
