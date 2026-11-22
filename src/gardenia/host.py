"""Per-host workstation inspection and setup (strictly isolated from Git repositories)."""

from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import sys
import tomllib
from typing import Any, Mapping

from gardenia import __version__
from gardenia.models import (
    CHECK_FORMAT,
    CLI_SPECS,
    CLI_WITH_PLUGINS,
    DEFAULT_TIMEOUT_SECONDS,
    GITHUB_OWNER,
    PLUGIN_PACKAGES,
    SUPPORTED_CLIENTS,
    CliSpec,
    Finding,
    GardeniaError,
    PlannedAction,
    compute_ok,
    extract_version,
    is_finite_number,
    parse_version,
    run_cmd,
    version_lt,
)

SETUP_COMMAND_TIMEOUT_SECONDS = 300.0
BROKER_PROBE_TIMEOUT_SECONDS = 10.0


def _effective_env(env: Mapping[str, str] | None) -> dict[str, str]:
    return dict(os.environ if env is None else env)


def _effective_home(env: Mapping[str, str]) -> Path:
    raw = env.get("HOME")
    if raw:
        return Path(raw)
    return Path.home()


def _effective_agroute_config_path(env: Mapping[str, str]) -> Path:
    override = env.get("AGROUTE_USER_CONFIG")
    if override:
        return Path(override)
    xdg = env.get("XDG_CONFIG_HOME")
    if xdg:
        return Path(xdg) / "agroute" / "config.toml"
    return _effective_home(env) / ".config" / "agroute" / "config.toml"


def parse_clients_arg(raw: str | None) -> list[str] | None:
    """Validate and deduplicate a comma-separated ``--clients`` argument."""

    if raw is None:
        return None
    items = [item.strip().lower() for item in raw.split(",") if item.strip()]
    if not items:
        raise GardeniaError(
            f"--clients requires at least one client from: {', '.join(SUPPORTED_CLIENTS)}",
            exit_code=2,
        )
    invalid = [item for item in items if item not in SUPPORTED_CLIENTS]
    if invalid:
        raise GardeniaError(
            f"Unknown client(s): {', '.join(invalid)}. Supported clients: {', '.join(SUPPORTED_CLIENTS)}",
            exit_code=2,
        )
    deduped: list[str] = []
    for item in items:
        if item not in deduped:
            deduped.append(item)
    return deduped


def _which(binary: str, env: Mapping[str, str]) -> str | None:
    return shutil.which(binary, path=env.get("PATH"))


def _inspect_cli(
    spec: CliSpec,
    *,
    is_linux: bool,
    with_agcoord: bool,
    env: Mapping[str, str],
    findings: list[Finding],
) -> dict[str, Any]:
    if spec.linux_only and not is_linux and not with_agcoord:
        return {
            "package": spec.package,
            "binary": spec.binary,
            "min_version": spec.min_version,
            "installed": False,
            "path": None,
            "version": None,
            "status": "skipped",
            "reason": "platform-unsupported",
        }

    path = _which(spec.binary, env)
    if path is None:
        findings.append(
            Finding(
                scope="host",
                severity="error",
                component=spec.package,
                message=f"Required host CLI '{spec.binary}' ({spec.package} >= {spec.min_version}) was not found on PATH.",
                remediation=f"uv tool install {spec.package}  # or: gardenia host setup",
            )
        )
        return {
            "package": spec.package,
            "binary": spec.binary,
            "min_version": spec.min_version,
            "installed": False,
            "path": None,
            "version": None,
            "status": "missing",
            "reason": "not-on-path",
        }

    result = run_cmd([path, "--version"], timeout=DEFAULT_TIMEOUT_SECONDS, env=env)
    combined = f"{result.stdout}\n{result.stderr}".strip()
    version = extract_version(combined)
    if not result.ok or version is None:
        detail = result.stderr.strip() or result.stdout.strip() or f"exit code {result.returncode}"
        findings.append(
            Finding(
                scope="host",
                severity="error",
                component=spec.package,
                message=f"Could not determine '{spec.binary}' version from '{path} --version': {detail}",
                remediation=f"uv tool install --upgrade {spec.package}",
            )
        )
        return {
            "package": spec.package,
            "binary": spec.binary,
            "min_version": spec.min_version,
            "installed": True,
            "path": path,
            "version": version,
            "status": "error",
            "reason": "version-probe-failed",
        }

    below = version_lt(version, spec.min_version)
    if below:
        findings.append(
            Finding(
                scope="host",
                severity="error",
                component=spec.package,
                message=f"Host CLI '{spec.binary}' version {version} is below minimum {spec.min_version}.",
                remediation=f"uv tool install --upgrade {spec.package}  # or: gardenia host setup --upgrade",
            )
        )
        return {
            "package": spec.package,
            "binary": spec.binary,
            "min_version": spec.min_version,
            "installed": True,
            "path": path,
            "version": version,
            "status": "outdated",
            "reason": "below-min-version",
        }

    return {
        "package": spec.package,
        "binary": spec.binary,
        "min_version": spec.min_version,
        "installed": True,
        "path": path,
        "version": version,
        "status": "ok",
        "reason": None,
    }


def _inspect_agcoord_broker(
    agc_info: dict[str, Any],
    *,
    is_linux: bool,
    with_agcoord: bool,
    env: Mapping[str, str],
    findings: list[Finding],
) -> dict[str, Any]:
    if not is_linux and not with_agcoord:
        return {
            "status": "skipped",
            "reason": "platform-unsupported",
            "reachable": False,
            "mode": None,
            "enforced": False,
            "protocol": None,
            "broker_pid": None,
            "capacities": {},
            "resource_bindings": {},
        }

    if not agc_info.get("installed") or not agc_info.get("path"):
        return {
            "status": "unavailable",
            "reason": "agc-not-installed",
            "reachable": False,
            "mode": None,
            "enforced": False,
            "protocol": None,
            "broker_pid": None,
            "capacities": {},
            "resource_bindings": {},
        }

    result = run_cmd([agc_info["path"], "--json", "list"], timeout=BROKER_PROBE_TIMEOUT_SECONDS, env=env)
    if not result.ok:
        detail = result.stderr.strip() or result.stdout.strip() or f"exit code {result.returncode}"
        findings.append(
            Finding(
                scope="host",
                severity="error",
                component="agcoord-broker",
                message=f"AGCoord host broker is not reachable via 'agc --json list': {detail}",
                remediation="agc host install --user  # or for cgroup-v2 enforcement: agc host install --download",
            )
        )
        return {
            "status": "unreachable",
            "reason": "timeout" if result.timed_out else "command-failed",
            "reachable": False,
            "mode": None,
            "enforced": False,
            "protocol": None,
            "broker_pid": None,
            "capacities": {},
            "resource_bindings": {},
            "error": detail,
        }

    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        findings.append(
            Finding(
                scope="host",
                severity="error",
                component="agcoord-broker",
                message=f"Could not parse JSON output from 'agc --json list': {exc}",
                remediation="agc --json list",
            )
        )
        return {
            "status": "error",
            "reason": "invalid-json",
            "reachable": False,
            "mode": None,
            "enforced": False,
            "protocol": None,
            "broker_pid": None,
            "capacities": {},
            "resource_bindings": {},
            "error": str(exc),
        }

    bindings = payload.get("resource_bindings")
    if not isinstance(bindings, dict):
        bindings = {}
    enforced_resources: list[str] = []
    for name, binding in bindings.items():
        if isinstance(binding, dict) and binding.get("backend") == "cgroup-v2":
            enforced_resources.append(str(name))
    enforced = bool(enforced_resources)
    mode = "cgroup-v2" if enforced else "admission-only"
    if not enforced:
        findings.append(
            Finding(
                scope="host",
                severity="info",
                component="agcoord-broker",
                message="AGCoord broker is running in user-mode admission-only accounting (no cgroup-v2 enforcement).",
                remediation="For kernel-enforced cgroup-v2 limits on supported Linux hosts, run: agc host install --download",
            )
        )

    return {
        "status": "ok",
        "reason": None,
        "reachable": True,
        "mode": mode,
        "enforced": enforced,
        "enforced_resources": sorted(enforced_resources),
        "protocol": payload.get("protocol"),
        "broker_pid": payload.get("broker_pid"),
        "capacities": payload.get("capacities") if isinstance(payload.get("capacities"), dict) else {},
        "resource_bindings": bindings,
    }


def _read_json_version(manifest_path: Path) -> str | None:
    try:
        data = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    raw = data.get("version")
    return str(raw).strip() if isinstance(raw, str) and raw.strip() else None


def _inspect_codex_plugins(
    home: Path,
    findings: list[Finding],
) -> tuple[dict[str, dict[str, Any]], str | None]:
    """Read Codex plugin configuration and cached manifests without running interactive TUI."""

    config_path = home / ".codex" / "config.toml"
    marketplaces: dict[str, Any] = {}
    plugins_cfg: dict[str, Any] = {}
    config_error: str | None = None

    if config_path.is_file():
        try:
            parsed = tomllib.loads(config_path.read_text(encoding="utf-8"))
        except (OSError, tomllib.TOMLDecodeError) as exc:
            config_error = str(exc)
            findings.append(
                Finding(
                    scope="host",
                    severity="error",
                    component="codex",
                    message=f"Could not parse Codex configuration at {config_path}: {exc}",
                    remediation=f"Fix TOML syntax in {config_path}",
                )
            )
        else:
            if isinstance(parsed.get("marketplaces"), dict):
                marketplaces = parsed["marketplaces"]
            if isinstance(parsed.get("plugins"), dict):
                plugins_cfg = parsed["plugins"]

    results: dict[str, dict[str, Any]] = {}
    for pkg in PLUGIN_PACKAGES:
        plugin_id = f"{pkg}@{pkg}"
        entry = plugins_cfg.get(plugin_id)
        configured = isinstance(entry, dict)
        enabled = bool(entry.get("enabled", True)) if configured else False
        marketplace_present = pkg in marketplaces

        cached_versions: list[tuple[tuple[int, int, int, int, str], str, Path]] = []
        cache_dir = home / ".codex" / "plugins" / "cache" / pkg / pkg
        if cache_dir.is_dir():
            for child in sorted(cache_dir.iterdir()):
                if not child.is_dir():
                    continue
                manifest = child / "plugin.json"
                if not manifest.is_file():
                    continue
                ver = _read_json_version(manifest)
                parsed_ver = parse_version(ver)
                if ver and parsed_ver is not None:
                    cached_versions.append((parsed_ver, ver, manifest))

        version: str | None = None
        version_source: str | None = None
        if cached_versions:
            cached_versions.sort(key=lambda item: item[0], reverse=True)
            version = cached_versions[0][1]
            version_source = str(cached_versions[0][2])
        else:
            tmp_manifest = home / ".codex" / ".tmp" / "marketplaces" / pkg / "plugin.json"
            if tmp_manifest.is_file():
                ver = _read_json_version(tmp_manifest)
                if ver:
                    version = ver
                    version_source = str(tmp_manifest)

        results[pkg] = {
            "installed": configured,
            "enabled": enabled,
            "version": version,
            "scope": "user" if configured else None,
            "marketplace_present": marketplace_present,
            "version_source": version_source,
        }
    return results, config_error


def _inspect_agy_plugins(
    binary_path: str,
    env: Mapping[str, str],
    findings: list[Finding],
) -> tuple[dict[str, dict[str, Any]], str | None]:
    result = run_cmd([binary_path, "plugin", "list", "--json"], timeout=DEFAULT_TIMEOUT_SECONDS, env=env)
    if not result.ok:
        detail = result.stderr.strip() or result.stdout.strip() or f"exit code {result.returncode}"
        findings.append(
            Finding(
                scope="host",
                severity="error",
                component="agy",
                message=f"Could not list Antigravity plugins via 'agy plugin list --json': {detail}",
                remediation="agy plugin list --json",
            )
        )
        return {}, detail

    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        findings.append(
            Finding(
                scope="host",
                severity="error",
                component="agy",
                message=f"Invalid JSON returned by 'agy plugin list --json': {exc}",
                remediation="agy plugin list --json",
            )
        )
        return {}, str(exc)

    entries = payload.get("plugins") if isinstance(payload, dict) else payload
    if not isinstance(entries, list):
        msg = "Unexpected JSON structure from 'agy plugin list --json' (expected list)."
        findings.append(
            Finding(
                scope="host",
                severity="error",
                component="agy",
                message=msg,
                remediation="agy plugin list --json",
            )
        )
        return {}, msg

    by_name: dict[str, dict[str, Any]] = {}
    for item in entries:
        if isinstance(item, dict) and isinstance(item.get("name"), str):
            by_name[item["name"]] = item

    results: dict[str, dict[str, Any]] = {}
    for pkg in PLUGIN_PACKAGES:
        entry = by_name.get(pkg)
        if entry is None:
            results[pkg] = {
                "installed": False,
                "enabled": False,
                "version": None,
                "scope": None,
            }
        else:
            raw_version = entry.get("version")
            results[pkg] = {
                "installed": True,
                "enabled": bool(entry.get("enabled", True)),
                "version": str(raw_version).strip() if isinstance(raw_version, str) and raw_version.strip() else None,
                "scope": "user",
            }
    return results, None


def _inspect_claude_plugins(
    binary_path: str,
    env: Mapping[str, str],
    findings: list[Finding],
) -> tuple[dict[str, dict[str, Any]], str | None]:
    result = run_cmd([binary_path, "plugin", "list", "--json"], timeout=DEFAULT_TIMEOUT_SECONDS, env=env)
    if not result.ok:
        detail = result.stderr.strip() or result.stdout.strip() or f"exit code {result.returncode}"
        findings.append(
            Finding(
                scope="host",
                severity="error",
                component="claude",
                message=f"Could not list Claude Code plugins via 'claude plugin list --json': {detail}",
                remediation="claude plugin list --json",
            )
        )
        return {}, detail

    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        findings.append(
            Finding(
                scope="host",
                severity="error",
                component="claude",
                message=f"Invalid JSON returned by 'claude plugin list --json': {exc}",
                remediation="claude plugin list --json",
            )
        )
        return {}, str(exc)

    entries = payload.get("plugins") if isinstance(payload, dict) else payload
    if not isinstance(entries, list):
        msg = "Unexpected JSON structure from 'claude plugin list --json' (expected list)."
        findings.append(
            Finding(
                scope="host",
                severity="error",
                component="claude",
                message=msg,
                remediation="claude plugin list --json",
            )
        )
        return {}, msg

    results: dict[str, dict[str, Any]] = {}
    for pkg in PLUGIN_PACKAGES:
        exact_id = f"{pkg}@{pkg}"
        candidates = [
            item
            for item in entries
            if isinstance(item, dict)
            and (item.get("id") == exact_id or str(item.get("id", "")).startswith(f"{pkg}@"))
        ]
        # Prefer exact id + user scope when multiple entries exist.
        candidates.sort(
            key=lambda item: (
                item.get("id") == exact_id,
                item.get("scope") == "user",
                bool(item.get("enabled", False)),
            ),
            reverse=True,
        )
        entry = candidates[0] if candidates else None
        if entry is None:
            results[pkg] = {
                "installed": False,
                "enabled": False,
                "version": None,
                "scope": None,
            }
        else:
            raw_version = entry.get("version")
            results[pkg] = {
                "installed": True,
                "enabled": bool(entry.get("enabled", True)),
                "version": str(raw_version).strip() if isinstance(raw_version, str) and raw_version.strip() else None,
                "scope": str(entry.get("scope")) if entry.get("scope") is not None else None,
            }
    return results, None


def _plugin_install_remediation(client: str, pkg: str) -> str:
    if client == "agy":
        return f"agy plugin install https://github.com/{GITHUB_OWNER}/{pkg}"
    if client == "claude":
        return f"claude plugin marketplace add {GITHUB_OWNER}/{pkg} && claude plugin install {pkg}@{pkg} --scope user"
    return f"codex plugin marketplace add {GITHUB_OWNER}/{pkg} && codex plugin add {pkg}@{pkg}"


def _plugin_upgrade_remediation(client: str, pkg: str) -> str:
    if client == "agy":
        return f"agy plugin uninstall {pkg} && agy plugin install https://github.com/{GITHUB_OWNER}/{pkg}"
    if client == "claude":
        return f"claude plugin marketplace update {pkg} && claude plugin update {pkg}@{pkg} --scope user"
    return f"codex plugin marketplace upgrade {pkg} && codex plugin add {pkg}@{pkg}"


def _plugin_enable_remediation(client: str, pkg: str) -> str:
    if client == "agy":
        return f"agy plugin enable {pkg}"
    if client == "claude":
        return f"claude plugin enable {pkg}@{pkg}"
    return f"Enable [plugins.\"{pkg}@{pkg}\"] in ~/.codex/config.toml or run: codex plugin add {pkg}@{pkg}"


def _evaluate_client_plugins(
    client: str,
    plugins: dict[str, dict[str, Any]],
    cli_versions: dict[str, str | None],
    findings: list[Finding],
) -> None:
    for pkg in PLUGIN_PACKAGES:
        info = plugins.get(pkg)
        if info is None:
            continue
        if not info.get("installed"):
            findings.append(
                Finding(
                    scope="host",
                    severity="error",
                    component=f"{client}:{pkg}",
                    message=f"User-scoped plugin '{pkg}' is not installed for client '{client}'.",
                    remediation=_plugin_install_remediation(client, pkg),
                )
            )
            continue
        if not info.get("enabled"):
            findings.append(
                Finding(
                    scope="host",
                    severity="warning",
                    component=f"{client}:{pkg}",
                    message=f"User-scoped plugin '{pkg}' is installed but disabled in client '{client}'.",
                    remediation=_plugin_enable_remediation(client, pkg),
                )
            )
        if pkg in CLI_WITH_PLUGINS:
            cli_ver = cli_versions.get(pkg)
            plugin_ver = info.get("version")
            if cli_ver and plugin_ver and cli_ver != plugin_ver:
                findings.append(
                    Finding(
                        scope="host",
                        severity="warning",
                        component=f"{client}:{pkg}",
                        message=(
                            f"Version drift in client '{client}' for '{pkg}': "
                            f"plugin is {plugin_ver} while host CLI is {cli_ver}."
                        ),
                        remediation=_plugin_upgrade_remediation(client, pkg),
                    )
                )


def _inspect_agroute_config(
    env: Mapping[str, str],
    findings: list[Finding],
) -> dict[str, Any]:
    config_path = _effective_agroute_config_path(env)
    if not config_path.is_file():
        return {
            "path": str(config_path),
            "exists": False,
            "valid": None,
            "agents": {},
            "error": None,
        }

    try:
        parsed = tomllib.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        findings.append(
            Finding(
                scope="host",
                severity="error",
                component="agroute-config",
                message=f"Host agroute config {config_path} has invalid TOML: {exc}",
                remediation=f"Fix TOML syntax in {config_path}",
            )
        )
        return {
            "path": str(config_path),
            "exists": True,
            "valid": False,
            "agents": {},
            "error": str(exc),
        }

    agents_raw = parsed.get("agents", {})
    if agents_raw is not None and not isinstance(agents_raw, dict):
        msg = "Expected [agents] to be a TOML table."
        findings.append(
            Finding(
                scope="host",
                severity="error",
                component="agroute-config",
                message=f"Invalid [agents] table in {config_path}: {msg}",
                remediation=f"Fix [agents] table in {config_path}",
            )
        )
        return {
            "path": str(config_path),
            "exists": True,
            "valid": False,
            "agents": {},
            "error": msg,
        }

    reserve_keys = (
        "plan",
        "enabled",
        "host_reserve_pct",
        "host_reserve_credit_usd",
        "min_remaining_credit_usd",
        "max_session_used_pct",
        "max_weekly_used_pct",
        "max_tier_used_pct",
    )
    pct_keys = ("host_reserve_pct", "max_session_used_pct", "max_weekly_used_pct", "max_tier_used_pct")
    credit_keys = ("host_reserve_credit_usd", "min_remaining_credit_usd")

    summary: dict[str, dict[str, Any]] = {}
    valid = True
    for agent_name, agent_cfg in (agents_raw or {}).items():
        if not isinstance(agent_cfg, dict):
            valid = False
            findings.append(
                Finding(
                    scope="host",
                    severity="error",
                    component="agroute-config",
                    message=f"Expected [agents.{agent_name}] in {config_path} to be a TOML table.",
                    remediation=f"Fix [agents.{agent_name}] in {config_path}",
                )
            )
            continue
        agent_summary: dict[str, Any] = {}
        for key in reserve_keys:
            if key in agent_cfg:
                agent_summary[key] = agent_cfg[key]
        for key in pct_keys:
            if key in agent_cfg:
                val = agent_cfg[key]
                if not is_finite_number(val) or not (0.0 <= float(val) <= 100.0):
                    valid = False
                    findings.append(
                        Finding(
                            scope="host",
                            severity="error",
                            component="agroute-config",
                            message=f"Invalid {key}={val!r} under [agents.{agent_name}] in {config_path} (expected 0..100).",
                            remediation=f"Set agents.{agent_name}.{key} between 0 and 100 in {config_path}",
                        )
                    )
        for key in credit_keys:
            if key in agent_cfg:
                val = agent_cfg[key]
                if not is_finite_number(val) or float(val) < 0.0:
                    valid = False
                    findings.append(
                        Finding(
                            scope="host",
                            severity="error",
                            component="agroute-config",
                            message=f"Invalid {key}={val!r} under [agents.{agent_name}] in {config_path} (expected >= 0).",
                            remediation=f"Set agents.{agent_name}.{key} to a non-negative number in {config_path}",
                        )
                    )
        summary[str(agent_name)] = agent_summary

    return {
        "path": str(config_path),
        "exists": True,
        "valid": valid,
        "agents": summary,
        "error": None if valid else "invalid-agent-settings",
    }


def check_host(
    *,
    clients: list[str] | None = None,
    with_agcoord: bool = False,
    strict: bool = False,
    env: Mapping[str, str] | None = None,
    platform: str | None = None,
) -> dict[str, Any]:
    """Perform read-only inspection of host CLIs, broker, plugins, and reserve config."""

    effective_env = _effective_env(env)
    effective_platform = sys.platform if platform is None else platform
    is_linux = effective_platform.startswith("linux")
    home = _effective_home(effective_env)
    findings: list[Finding] = []

    clis: dict[str, dict[str, Any]] = {}
    for spec in CLI_SPECS:
        clis[spec.package] = _inspect_cli(
            spec,
            is_linux=is_linux,
            with_agcoord=with_agcoord,
            env=effective_env,
            findings=findings,
        )

    cli_versions = {pkg: info.get("version") for pkg, info in clis.items()}
    broker = _inspect_agcoord_broker(
        clis["agcoord"],
        is_linux=is_linux,
        with_agcoord=with_agcoord,
        env=effective_env,
        findings=findings,
    )

    detected_clients = [name for name in SUPPORTED_CLIENTS if _which(name, effective_env) is not None]
    target_clients = list(clients) if clients is not None else list(detected_clients)

    clients_report: dict[str, dict[str, Any]] = {}
    if not target_clients:
        findings.append(
            Finding(
                scope="host",
                severity="warning",
                component="clients",
                message="No supported coding-agent clients (codex, claude, agy) were detected on PATH.",
                remediation="Install codex, claude, or agy, then run: gardenia host setup",
            )
        )

    for client in target_clients:
        path = _which(client, effective_env)
        if path is None:
            findings.append(
                Finding(
                    scope="host",
                    severity="error",
                    component=client,
                    message=f"Requested agent client '{client}' was not found on PATH.",
                    remediation=f"Install '{client}' or omit it from --clients.",
                )
            )
            clients_report[client] = {
                "installed": False,
                "path": None,
                "plugins": {},
                "error": "not-on-path",
            }
            continue

        if client == "codex":
            plugins, err = _inspect_codex_plugins(home, findings)
        elif client == "agy":
            plugins, err = _inspect_agy_plugins(path, effective_env, findings)
        else:
            plugins, err = _inspect_claude_plugins(path, effective_env, findings)

        if err is None:
            _evaluate_client_plugins(client, plugins, cli_versions, findings)

        clients_report[client] = {
            "installed": True,
            "path": path,
            "plugins": plugins,
            "error": err,
        }

    agroute_config = _inspect_agroute_config(effective_env, findings)
    ok = compute_ok(findings, strict=strict)

    host_data = {
        "platform": effective_platform,
        "home": str(home),
        "detected_clients": detected_clients,
        "clis": clis,
        "broker": broker,
        "clients": clients_report,
        "agroute_config": agroute_config,
    }

    return {
        "format": CHECK_FORMAT,
        "version": __version__,
        "scope": "host",
        "ok": ok,
        "strict": strict,
        "host": host_data,
        "findings": [f.to_dict() for f in findings],
        "_findings": findings,
    }


def _select_installer(installer: str, env: Mapping[str, str]) -> str:
    if installer == "auto":
        if _which("uv", env):
            return "uv"
        if _which("pipx", env):
            return "pipx"
        raise GardeniaError(
            "Neither 'uv' nor 'pipx' was found on PATH. Install uv (recommended) or pipx first.",
            exit_code=2,
        )
    if installer in ("uv", "pipx"):
        if not _which(installer, env):
            raise GardeniaError(
                f"Requested installer '{installer}' was not found on PATH.",
                exit_code=2,
            )
        return installer
    raise GardeniaError(f"Unsupported installer '{installer}'. Use auto, uv, or pipx.", exit_code=2)


def _render_agroute_reserve_config(
    *,
    reserve_pct: float | None,
    host_reserve_credit: float | None,
    min_remaining_credit: float | None,
) -> str:
    lines = [
        "# Host-wide agroute reserve defaults generated by gardenia host setup.",
        "",
    ]
    for agent in ("codex", "claude", "agy"):
        lines.append(f"[agents.{agent}]")
        if reserve_pct is not None:
            lines.append(f"host_reserve_pct = {float(reserve_pct):g}")
        if host_reserve_credit is not None:
            lines.append(f"host_reserve_credit_usd = {float(host_reserve_credit):g}")
        if min_remaining_credit is not None:
            lines.append(f"min_remaining_credit_usd = {float(min_remaining_credit):g}")
        lines.append("")
    return "\n".join(lines)


def setup_host(
    *,
    clients: list[str] | None = None,
    installer: str = "auto",
    broker: str = "auto",
    force_broker: bool = False,
    upgrade: bool = False,
    reserve_pct: float | None = None,
    host_reserve_credit: float | None = None,
    min_remaining_credit: float | None = None,
    overwrite_config: bool = False,
    with_agcoord: bool = False,
    dry_run: bool = False,
    env: Mapping[str, str] | None = None,
    platform: str | None = None,
) -> dict[str, Any]:
    """Plan and (unless dry_run) execute per-host CLI, broker, plugin, and reserve setup."""

    if broker not in ("auto", "user", "enforced", "skip"):
        raise GardeniaError(f"Invalid --broker mode '{broker}'. Use auto, user, enforced, or skip.", exit_code=2)
    if reserve_pct is not None and (not is_finite_number(reserve_pct) or not (0.0 <= float(reserve_pct) <= 100.0)):
        raise GardeniaError("--reserve-pct must be a finite number between 0 and 100.", exit_code=2)
    if host_reserve_credit is not None and (not is_finite_number(host_reserve_credit) or float(host_reserve_credit) < 0.0):
        raise GardeniaError("--host-reserve-credit must be a non-negative finite number.", exit_code=2)
    if min_remaining_credit is not None and (not is_finite_number(min_remaining_credit) or float(min_remaining_credit) < 0.0):
        raise GardeniaError("--min-remaining-credit must be a non-negative finite number.", exit_code=2)

    effective_env = _effective_env(env)
    effective_platform = sys.platform if platform is None else platform
    is_linux = effective_platform.startswith("linux")

    state = check_host(
        clients=clients,
        with_agcoord=with_agcoord,
        env=effective_env,
        platform=effective_platform,
    )["host"]

    # Validate explicit --clients exist on PATH before mutating anything.
    target_clients = list(clients) if clients is not None else list(state["detected_clients"])
    for client in target_clients:
        if not state["clients"].get(client, {}).get("installed"):
            raise GardeniaError(
                f"Requested client '{client}' is not installed on PATH.",
                exit_code=2,
            )

    # Validate broker preconditions upfront before any mutation.
    broker_state = state["broker"]
    broker_enforced = bool(broker_state.get("enforced"))
    broker_reachable = bool(broker_state.get("reachable"))
    include_agcoord = is_linux or with_agcoord
    if include_agcoord and broker == "user" and broker_enforced and not force_broker:
        raise GardeniaError(
            "Refusing '--broker user': an active cgroup-v2 enforced AGCoord broker is already running "
            "on this host. Use '--broker auto' (to preserve it), '--broker enforced' (to upgrade it), "
            "or pass '--force-broker' to override.",
            exit_code=2,
        )

    # Validate host reserve config overwrite preconditions upfront.
    write_reserve_config = any(
        val is not None for val in (reserve_pct, host_reserve_credit, min_remaining_credit)
    )
    reserve_config_path = _effective_agroute_config_path(effective_env)
    if write_reserve_config and reserve_config_path.exists() and not overwrite_config:
        raise GardeniaError(
            f"Refusing to overwrite existing host agroute config at {reserve_config_path}. "
            "Pass --overwrite-config to replace it.",
            exit_code=2,
        )

    actions: list[PlannedAction] = []

    # 1. Python CLI tools.
    target_specs = [spec for spec in CLI_SPECS if not spec.linux_only or include_agcoord]
    needs_installer = any(
        (not state["clis"][spec.package]["installed"])
        or state["clis"][spec.package]["status"] != "ok"
        or upgrade
        for spec in target_specs
    )
    chosen_installer: str | None = None
    if needs_installer:
        chosen_installer = _select_installer(installer, effective_env)
    elif installer in ("uv", "pipx"):
        chosen_installer = installer
    else:
        chosen_installer = "uv" if _which("uv", effective_env) else ("pipx" if _which("pipx", effective_env) else None)

    agcoord_cli_mutated = False
    for spec in target_specs:
        cli_info = state["clis"][spec.package]
        installed = bool(cli_info["installed"])
        status = cli_info["status"]
        if not installed:
            cmd = (
                ["uv", "tool", "install", spec.package]
                if chosen_installer == "uv"
                else ["pipx", "install", spec.package]
            )
            actions.append(
                PlannedAction(
                    kind="command",
                    component=spec.package,
                    description=f"Install {spec.package} CLI via {chosen_installer}.",
                    command=cmd,
                )
            )
            if spec.package == "agcoord":
                agcoord_cli_mutated = True
        elif upgrade or status != "ok":
            cmd = (
                ["uv", "tool", "install", "--upgrade", spec.package]
                if chosen_installer == "uv"
                else ["pipx", "upgrade", spec.package]
            )
            actions.append(
                PlannedAction(
                    kind="command",
                    component=spec.package,
                    description=f"Upgrade {spec.package} CLI via {chosen_installer}.",
                    command=cmd,
                )
            )
            if spec.package == "agcoord":
                agcoord_cli_mutated = True
        else:
            actions.append(
                PlannedAction(
                    kind="skip",
                    component=spec.package,
                    description=f"{spec.package} {cli_info['version']} already satisfies >= {spec.min_version}.",
                    status="skipped",
                )
            )

    # 2. AGCoord host broker (Linux or --with-agcoord).
    if not include_agcoord or broker == "skip":
        reason = "platform-unsupported" if not include_agcoord else "--broker skip"
        actions.append(
            PlannedAction(
                kind="skip",
                component="agcoord-broker",
                description=f"Skipping AGCoord broker step ({reason}).",
                status="skipped",
            )
        )
    elif broker == "auto":
        if broker_enforced:
            note = (
                "Preserving active cgroup-v2 enforced AGCoord broker untouched "
                "(pass --broker enforced to upgrade a managed enforced broker)."
            )
            actions.append(
                PlannedAction(
                    kind="skip",
                    component="agcoord-broker",
                    description=note,
                    status="skipped",
                )
            )
        elif not broker_reachable or upgrade or agcoord_cli_mutated:
            actions.append(
                PlannedAction(
                    kind="command",
                    component="agcoord-broker",
                    description="Install or refresh unprivileged user-mode AGCoord broker.",
                    command=["agc", "host", "install", "--user"],
                )
            )
        else:
            actions.append(
                PlannedAction(
                    kind="skip",
                    component="agcoord-broker",
                    description="User-mode AGCoord broker is already reachable.",
                    status="skipped",
                )
            )
    elif broker == "user":
        actions.append(
            PlannedAction(
                kind="command",
                component="agcoord-broker",
                description="Install unprivileged user-mode AGCoord broker.",
                command=["agc", "host", "install", "--user"],
            )
        )
    elif broker == "enforced":
        subcmd = "upgrade" if (upgrade and broker_enforced) else "install"
        actions.append(
            PlannedAction(
                kind="command",
                component="agcoord-broker",
                description=f"Run privileged cgroup-v2 AGCoord broker {subcmd}.",
                command=["agc", "host", subcmd, "--download"],
            )
        )

    # 3. Native client plugins across target clients.
    cli_versions = {pkg: info.get("version") for pkg, info in state["clis"].items()}
    for client in target_clients:
        client_plugins = state["clients"].get(client, {}).get("plugins", {})
        for pkg in PLUGIN_PACKAGES:
            pinfo = client_plugins.get(pkg, {})
            installed = bool(pinfo.get("installed"))
            enabled = bool(pinfo.get("enabled"))
            plugin_ver = pinfo.get("version")
            cli_ver = cli_versions.get(pkg) if pkg in CLI_WITH_PLUGINS else None
            drifted = bool(cli_ver and plugin_ver and cli_ver != plugin_ver)
            needs_refresh = upgrade or drifted

            if client == "agy":
                url = f"https://github.com/{GITHUB_OWNER}/{pkg}"
                if not installed:
                    actions.append(
                        PlannedAction(
                            kind="command",
                            component=f"agy:{pkg}",
                            description=f"Install Antigravity plugin '{pkg}'.",
                            command=["agy", "plugin", "install", url],
                        )
                    )
                elif needs_refresh:
                    actions.append(
                        PlannedAction(
                            kind="command",
                            component=f"agy:{pkg}",
                            description=f"Remove previous Antigravity plugin '{pkg}' before reinstall.",
                            command=["agy", "plugin", "uninstall", pkg],
                        )
                    )
                    actions.append(
                        PlannedAction(
                            kind="command",
                            component=f"agy:{pkg}",
                            description=f"Reinstall Antigravity plugin '{pkg}' from {url}.",
                            command=["agy", "plugin", "install", url],
                        )
                    )
                elif not enabled:
                    actions.append(
                        PlannedAction(
                            kind="command",
                            component=f"agy:{pkg}",
                            description=f"Enable Antigravity plugin '{pkg}'.",
                            command=["agy", "plugin", "enable", pkg],
                        )
                    )
                else:
                    actions.append(
                        PlannedAction(
                            kind="skip",
                            component=f"agy:{pkg}",
                            description=f"Antigravity plugin '{pkg}' ({plugin_ver or 'installed'}) is up to date.",
                            status="skipped",
                        )
                    )
            elif client == "claude":
                plugin_id = f"{pkg}@{pkg}"
                if not installed:
                    actions.append(
                        PlannedAction(
                            kind="command",
                            component=f"claude:{pkg}",
                            description=f"Register Claude Code marketplace '{GITHUB_OWNER}/{pkg}'.",
                            command=["claude", "plugin", "marketplace", "add", f"{GITHUB_OWNER}/{pkg}"],
                        )
                    )
                    actions.append(
                        PlannedAction(
                            kind="command",
                            component=f"claude:{pkg}",
                            description=f"Install Claude Code user plugin '{plugin_id}'.",
                            command=["claude", "plugin", "install", plugin_id, "--scope", "user"],
                        )
                    )
                elif needs_refresh:
                    actions.append(
                        PlannedAction(
                            kind="command",
                            component=f"claude:{pkg}",
                            description=f"Refresh Claude Code marketplace '{pkg}'.",
                            command=["claude", "plugin", "marketplace", "update", pkg],
                        )
                    )
                    actions.append(
                        PlannedAction(
                            kind="command",
                            component=f"claude:{pkg}",
                            description=f"Update Claude Code user plugin '{plugin_id}'.",
                            command=["claude", "plugin", "update", plugin_id, "--scope", "user"],
                        )
                    )
                elif not enabled:
                    actions.append(
                        PlannedAction(
                            kind="command",
                            component=f"claude:{pkg}",
                            description=f"Enable Claude Code plugin '{plugin_id}'.",
                            command=["claude", "plugin", "enable", plugin_id],
                        )
                    )
                else:
                    actions.append(
                        PlannedAction(
                            kind="skip",
                            component=f"claude:{pkg}",
                            description=f"Claude Code plugin '{plugin_id}' ({plugin_ver or 'installed'}) is up to date.",
                            status="skipped",
                        )
                    )
            elif client == "codex":
                plugin_id = f"{pkg}@{pkg}"
                marketplace_present = bool(pinfo.get("marketplace_present"))
                if not installed:
                    if not marketplace_present:
                        actions.append(
                            PlannedAction(
                                kind="command",
                                component=f"codex:{pkg}",
                                description=f"Register Codex marketplace '{GITHUB_OWNER}/{pkg}'.",
                                command=["codex", "plugin", "marketplace", "add", f"{GITHUB_OWNER}/{pkg}"],
                            )
                        )
                    actions.append(
                        PlannedAction(
                            kind="command",
                            component=f"codex:{pkg}",
                            description=f"Install Codex plugin '{plugin_id}'.",
                            command=["codex", "plugin", "add", plugin_id],
                        )
                    )
                elif needs_refresh:
                    if marketplace_present:
                        actions.append(
                            PlannedAction(
                                kind="command",
                                component=f"codex:{pkg}",
                                description=f"Upgrade Codex marketplace '{pkg}'.",
                                command=["codex", "plugin", "marketplace", "upgrade", pkg],
                            )
                        )
                    else:
                        actions.append(
                            PlannedAction(
                                kind="command",
                                component=f"codex:{pkg}",
                                description=f"Register Codex marketplace '{GITHUB_OWNER}/{pkg}'.",
                                command=["codex", "plugin", "marketplace", "add", f"{GITHUB_OWNER}/{pkg}"],
                            )
                        )
                    actions.append(
                        PlannedAction(
                            kind="command",
                            component=f"codex:{pkg}",
                            description=f"Refresh Codex plugin '{plugin_id}'.",
                            command=["codex", "plugin", "add", plugin_id],
                        )
                    )
                else:
                    actions.append(
                        PlannedAction(
                            kind="skip",
                            component=f"codex:{pkg}",
                            description=f"Codex plugin '{plugin_id}' ({plugin_ver or 'installed'}) is up to date.",
                            status="skipped",
                        )
                    )

    # 4. Optional host agroute reserve configuration.
    rendered_reserve_toml: str | None = None
    if write_reserve_config:
        rendered_reserve_toml = _render_agroute_reserve_config(
            reserve_pct=reserve_pct,
            host_reserve_credit=host_reserve_credit,
            min_remaining_credit=min_remaining_credit,
        )
        actions.append(
            PlannedAction(
                kind="write_file",
                component="agroute-config",
                description=f"Write host agroute reserve configuration to {reserve_config_path}.",
                path=str(reserve_config_path),
            )
        )

    # Execute unless --dry-run.
    ok = True
    if not dry_run:
        for action in actions:
            if action.kind == "skip":
                continue
            if action.kind == "command" and action.command is not None:
                res = run_cmd(action.command, timeout=SETUP_COMMAND_TIMEOUT_SECONDS, env=effective_env)
                action.returncode = res.returncode
                action.stdout = res.stdout.strip()
                action.stderr = res.stderr.strip()
                if res.ok:
                    action.status = "executed"
                else:
                    action.status = "failed"
                    ok = False
                    break
            elif action.kind == "write_file" and action.path is not None and rendered_reserve_toml is not None:
                target = Path(action.path)
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(rendered_reserve_toml, encoding="utf-8")
                action.status = "executed"

    return {
        "scope": "host",
        "ok": ok,
        "dry_run": dry_run,
        "installer": chosen_installer,
        "broker_mode": broker,
        "clients": target_clients,
        "actions": [action.to_dict() for action in actions],
        "agroute_config_preview": rendered_reserve_toml if (dry_run and rendered_reserve_toml) else None,
    }
