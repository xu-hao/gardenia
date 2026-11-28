"""Per-host workstation inspection and setup (strictly isolated from Git repositories)."""

from __future__ import annotations

from dataclasses import dataclass, replace
import json
import os
from pathlib import Path
import re
import shutil
import sys
import tomllib
from typing import Any, Mapping

from gardenia import __version__
from gardenia.models import (
    BUILTIN_PLUGINS,
    BUILTIN_TOOLS,
    CHECK_FORMAT,
    DEFAULT_TIMEOUT_SECONDS,
    ENTRY_NAME_RE,
    PLUGIN_SNIPPET_FILENAME,
    RESERVED_ENTRY_NAMES,
    SUPPORTED_CLIENTS,
    CliSpec,
    Finding,
    GardeniaError,
    PlannedAction,
    PluginSpec,
    compute_ok,
    extract_version,
    is_finite_number,
    parse_version,
    run_cmd,
    version_lt,
)
from gardenia.repo import extract_sibling_block, normalize_snippet, render_sibling_block

SETUP_COMMAND_TIMEOUT_SECONDS = 300.0
BROKER_PROBE_TIMEOUT_SECONDS = 30.0

HOST_CONFIG_KEYS = frozenset({"tools", "plugins"})
HOST_TOOL_KEYS = frozenset({"name", "install_target", "package", "binary", "min_version", "linux_only"})
HOST_PLUGIN_KEYS = frozenset({"name", "source", "clients", "cli"})

_LOCAL_PREFIXES = ("/", "./", "../", "~/")
_GIT_URL_PREFIXES = ("git+ssh://", "git+https://", "ssh://", "https://")
_SCP_SOURCE_RE = re.compile(r"^(?P<user>[A-Za-z0-9._-]+)@(?P<host>[A-Za-z0-9.-]+):(?P<path>[^/].*)$")
_GITHUB_SOURCE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9-]*/[A-Za-z0-9._-]+$")
_PACKAGE_TARGET_RE = re.compile(
    r"^(?P<dist>[A-Za-z0-9][A-Za-z0-9._-]*)(?:\[[A-Za-z0-9._,-]+\])?(?:(?:[<>]=?|[=!~]=)[A-Za-z0-9.*+!,<>=~-]+)?$"
)


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


@dataclass(frozen=True)
class HostConfig:
    """Built-in managed stack merged with the user's host ``config.toml``."""

    path: Path
    exists: bool
    tools: tuple[CliSpec, ...] = BUILTIN_TOOLS
    plugins: tuple[PluginSpec, ...] = BUILTIN_PLUGINS
    user_tools: tuple[str, ...] = ()
    user_plugins: tuple[str, ...] = ()

    def tool(self, name: str | None) -> CliSpec | None:
        return next((spec for spec in self.tools if spec.name == name), None)

    def plugin(self, name: str) -> PluginSpec | None:
        return next((spec for spec in self.plugins if spec.name == name), None)

    def plugins_for(self, client: str) -> tuple[PluginSpec, ...]:
        return tuple(spec for spec in self.plugins if client in spec.clients)

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": str(self.path),
            "exists": self.exists,
            "user_tools": list(self.user_tools),
            "user_plugins": list(self.user_plugins),
        }


def _expand_user(raw: str, env: Mapping[str, str]) -> Path:
    """Expand a leading ``~`` against the effective ``HOME`` (never the ambient one)."""

    if raw == "~" or raw.startswith("~/"):
        return _effective_home(env) / raw[2:]
    return Path(raw)


def effective_host_config_path(env: Mapping[str, str] | None = None) -> Path:
    """Resolve the host tool and plugin configuration path."""

    effective_env = _effective_env(env)
    override = effective_env.get("GARDENIA_HOST_CONFIG", "").strip()
    if override:
        return _expand_user(override, effective_env)
    xdg = effective_env.get("XDG_CONFIG_HOME", "").strip()
    if xdg:
        return _expand_user(xdg, effective_env) / "gardenia" / "config.toml"
    return _effective_home(effective_env) / ".config" / "gardenia" / "config.toml"


def _classify_source(value: str) -> str | None:
    """Classify an install source as ``local``, ``git``, ``scp``, ``github``, or ``package``."""

    if value.startswith("-") or any(ord(char) < 32 for char in value):
        return None
    if value in (".", "..", "~") or value.startswith(_LOCAL_PREFIXES):
        return "local"
    if any(char.isspace() for char in value):
        return None
    if value.startswith(_GIT_URL_PREFIXES):
        return "git"
    if _SCP_SOURCE_RE.match(value):
        return "scp"
    if _GITHUB_SOURCE_RE.match(value):
        return "github"
    if _PACKAGE_TARGET_RE.match(value):
        return "package"
    return None


def _resolve_local_source(value: str, *, config_dir: Path, env: Mapping[str, str]) -> str:
    path = _expand_user(value, env)
    if not path.is_absolute():
        path = config_dir / path
    return os.path.normpath(os.path.abspath(path))


def _tool_install_target(value: str, kind: str, *, config_dir: Path, env: Mapping[str, str]) -> str:
    """Normalize a configured tool source into a ``uv`` / ``pipx`` install argument."""

    if kind == "local":
        return _resolve_local_source(value, config_dir=config_dir, env=env)
    if kind == "github":
        return f"git+https://github.com/{value.removesuffix('.git')}.git"
    if kind == "scp":
        match = _SCP_SOURCE_RE.match(value)
        assert match is not None
        return f"git+ssh://{match['user']}@{match['host']}/{match['path']}"
    if kind == "git" and (value.startswith("ssh://") or (value.startswith("https://") and value.endswith(".git"))):
        return f"git+{value}"
    return value


def _config_error(path: Path, message: str) -> GardeniaError:
    return GardeniaError(f"Invalid host gardenia configuration ({path}): {message}", exit_code=2)


def _config_entries(data: Mapping[str, Any], key: str, allowed: frozenset[str], path: Path) -> list[dict[str, Any]]:
    raw = data.get(key, [])
    if not isinstance(raw, list) or any(not isinstance(item, dict) for item in raw):
        raise _config_error(path, f"'{key}' must be an array of tables ([[{key}]]).")
    seen: set[str] = set()
    for item in raw:
        name = item.get("name")
        if not isinstance(name, str) or not ENTRY_NAME_RE.fullmatch(name) or name in RESERVED_ENTRY_NAMES:
            raise _config_error(
                path,
                f"every [[{key}]] entry needs a 'name' of letters, digits, '.', '_', or '-' "
                f"(not 'gardenia'); got {name!r}.",
            )
        unknown = sorted(set(item) - allowed)
        if unknown:
            raise _config_error(
                path,
                f"unknown key(s) in [[{key}]] '{name}': {', '.join(unknown)}. "
                f"Allowed keys: {', '.join(sorted(allowed))}.",
            )
        if name in seen:
            raise _config_error(path, f"duplicate [[{key}]] entry '{name}'.")
        seen.add(name)
    return raw


def _config_string(item: Mapping[str, Any], key: str, path: Path) -> str:
    value = item[key]
    if not isinstance(value, str) or not value.strip() or value.startswith("-"):
        raise _config_error(path, f"'{key}' for '{item['name']}' must be a non-empty string not starting with '-'.")
    return value.strip()


def _parse_tool_entry(
    item: Mapping[str, Any],
    base: CliSpec | None,
    *,
    path: Path,
    env: Mapping[str, str],
) -> CliSpec:
    name = item["name"]
    spec = base or CliSpec(name=name, binary=name, min_version="0.0.0")
    changes: dict[str, Any] = {}
    if "install_target" in item and "package" in item:
        raise _config_error(path, f"set only one of 'install_target' or 'package' for tool '{name}'.")
    for key in ("install_target", "package"):
        if key in item:
            value = _config_string(item, key, path)
            kind = _classify_source(value)
            if kind is None:
                raise _config_error(
                    path,
                    f"'{key}' for tool '{name}' must be a PyPI name, git+ssh:// or git+https:// URL, "
                    "GitHub owner/repo, or a local path starting with '/', './', '../', or '~/'.",
                )
            changes["install_target"] = _tool_install_target(value, kind, config_dir=path.parent, env=env)
    if "binary" in item:
        binary = _config_string(item, "binary", path)
        if any(char.isspace() for char in binary):
            raise _config_error(path, f"'binary' for tool '{name}' must not contain whitespace.")
        changes["binary"] = binary
    if "min_version" in item:
        min_version = _config_string(item, "min_version", path)
        if parse_version(min_version) is None:
            raise _config_error(path, f"'min_version' for tool '{name}' must be MAJOR.MINOR.PATCH.")
        changes["min_version"] = min_version.lstrip("v")
    if "linux_only" in item:
        if not isinstance(item["linux_only"], bool):
            raise _config_error(path, f"'linux_only' for tool '{name}' must be a boolean.")
        changes["linux_only"] = item["linux_only"]
    # Never default a new tool to a public index package that merely shares its name.
    if base is None and "install_target" not in changes:
        raise _config_error(path, f"tool '{name}' requires an 'install_target' (or 'package').")
    return replace(spec, **changes)


def _parse_plugin_entry(
    item: Mapping[str, Any],
    base: PluginSpec | None,
    *,
    path: Path,
    env: Mapping[str, str],
) -> PluginSpec:
    name = item["name"]
    changes: dict[str, Any] = {}
    if "source" in item:
        value = _config_string(item, "source", path)
        kind = _classify_source(value)
        if kind not in ("local", "git", "scp", "github"):
            raise _config_error(
                path,
                f"'source' for plugin '{name}' must be a Git URL, GitHub owner/repo, or a local path "
                "starting with '/', './', '../', or '~/'.",
            )
        if kind == "local":
            value = _resolve_local_source(value, config_dir=path.parent, env=env)
        changes["source"] = value
    elif base is None:
        raise _config_error(path, f"plugin '{name}' requires a 'source'.")
    if "clients" in item:
        clients = item["clients"]
        if (
            not isinstance(clients, list)
            or not clients
            or any(client not in SUPPORTED_CLIENTS for client in clients)
            or len(set(clients)) != len(clients)
        ):
            raise _config_error(
                path,
                f"'clients' for plugin '{name}' must be a non-empty list of unique values from: "
                f"{', '.join(SUPPORTED_CLIENTS)}.",
            )
        changes["clients"] = tuple(clients)
    if "cli" in item:
        changes["cli"] = _config_string(item, "cli", path)
    if base is None:
        return PluginSpec(name=name, **changes)
    return replace(base, **changes)


def load_host_config(env: Mapping[str, str] | None = None) -> HostConfig:
    """Load the host configuration and merge it over the built-in tools and plugins.

    An entry named like a built-in overrides only the keys it sets; other entries are appended.
    """

    effective_env = _effective_env(env)
    path = effective_host_config_path(effective_env)
    if path.is_dir():
        raise _config_error(path, "path is a directory.")
    if not path.exists():
        return HostConfig(path=path, exists=False)
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
        raise _config_error(path, str(exc)) from exc

    unknown = sorted(set(data) - HOST_CONFIG_KEYS)
    if unknown:
        raise _config_error(path, f"unknown key(s): {', '.join(unknown)}. Allowed keys: plugins, tools.")

    tools = {spec.name: spec for spec in BUILTIN_TOOLS}
    user_tools = _config_entries(data, "tools", HOST_TOOL_KEYS, path)
    for item in user_tools:
        tools[item["name"]] = _parse_tool_entry(item, tools.get(item["name"]), path=path, env=effective_env)

    plugins = {spec.name: spec for spec in BUILTIN_PLUGINS}
    user_plugins = _config_entries(data, "plugins", HOST_PLUGIN_KEYS, path)
    for item in user_plugins:
        plugin = _parse_plugin_entry(item, plugins.get(item["name"]), path=path, env=effective_env)
        if plugin.cli is not None and plugin.cli not in tools:
            raise _config_error(path, f"plugin '{plugin.name}' references unknown tool cli = '{plugin.cli}'.")
        plugins[plugin.name] = plugin

    return HostConfig(
        path=path,
        exists=True,
        tools=tuple(tools.values()),
        plugins=tuple(plugins.values()),
        user_tools=tuple(item["name"] for item in user_tools),
        user_plugins=tuple(item["name"] for item in user_plugins),
    )


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


def _tool_applies(spec: CliSpec, *, is_linux: bool, with_agcoord: bool) -> bool:
    return not spec.linux_only or is_linux or (with_agcoord and spec.name == "agcoord")


def _tool_identity(spec: CliSpec) -> dict[str, str]:
    return {"package": spec.name, "install_target": spec.target}


def _inspect_cli(
    spec: CliSpec,
    *,
    is_linux: bool,
    with_agcoord: bool,
    env: Mapping[str, str],
    findings: list[Finding],
) -> dict[str, Any]:
    if not _tool_applies(spec, is_linux=is_linux, with_agcoord=with_agcoord):
        return {
            **_tool_identity(spec),
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
                component=spec.name,
                message=f"Required host CLI '{spec.binary}' ({spec.name} >= {spec.min_version}) was not found on PATH.",
                remediation=f"uv tool install {spec.target}  # or: gardenia host setup",
            )
        )
        return {
            **_tool_identity(spec),
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
                component=spec.name,
                message=f"Could not determine '{spec.binary}' version from '{path} --version': {detail}",
                remediation=f"uv tool install --upgrade {spec.target}",
            )
        )
        return {
            **_tool_identity(spec),
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
                component=spec.name,
                message=f"Host CLI '{spec.binary}' version {version} is below minimum {spec.min_version}.",
                remediation=f"uv tool install --upgrade {spec.target}  # or: gardenia host setup --upgrade",
            )
        )
        return {
            **_tool_identity(spec),
            "binary": spec.binary,
            "min_version": spec.min_version,
            "installed": True,
            "path": path,
            "version": version,
            "status": "outdated",
            "reason": "below-min-version",
        }

    return {
        **_tool_identity(spec),
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

    result = run_cmd([agc_info["path"], "--json", "list", "--limit", "1"], timeout=BROKER_PROBE_TIMEOUT_SECONDS, env=env)
    if not result.ok:
        detail = result.stderr.strip() or result.stdout.strip() or f"exit code {result.returncode}"
        if result.timed_out:
            findings.append(
                Finding(
                    scope="host",
                    severity="error",
                    component="agcoord-broker",
                    message=f"AGCoord host broker probe timed out via 'agc --json list --limit 1': {detail}",
                    remediation="agc --json list --limit 1",
                )
            )
        else:
            findings.append(
                Finding(
                    scope="host",
                    severity="error",
                    component="agcoord-broker",
                    message=f"AGCoord host broker is not reachable via 'agc --json list --limit 1': {detail}",
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
                message=f"Could not parse JSON output from 'agc --json list --limit 1': {exc}",
                remediation="agc --json list --limit 1",
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
    specs: tuple[PluginSpec, ...],
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
    for pkg in [spec.name for spec in specs]:
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
        install_path: str | None = None
        if cached_versions:
            cached_versions.sort(key=lambda item: item[0], reverse=True)
            version = cached_versions[0][1]
            version_source = str(cached_versions[0][2])
            install_path = str(cached_versions[0][2].parent)
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
            "install_path": install_path,
        }
    return results, config_error


def _optional_path(value: Any) -> str | None:
    return value if isinstance(value, str) and value.strip() else None


def _inspect_agy_plugins(
    binary_path: str,
    env: Mapping[str, str],
    findings: list[Finding],
    specs: tuple[PluginSpec, ...],
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
    for pkg in [spec.name for spec in specs]:
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
                "install_path": _optional_path(entry.get("path")),
            }
    return results, None


def _inspect_claude_plugins(
    binary_path: str,
    env: Mapping[str, str],
    findings: list[Finding],
    specs: tuple[PluginSpec, ...],
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
    for pkg in [spec.name for spec in specs]:
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
                "install_path": _optional_path(entry.get("installPath")),
            }
    return results, None


def _marketplace_source(plugin: PluginSpec) -> str:
    """Return the ``claude`` / ``codex`` ``plugin marketplace add`` argument."""

    return plugin.source.removeprefix("git+")


def _agy_source(plugin: PluginSpec) -> str:
    """Return the ``agy plugin install`` argument."""

    if _classify_source(plugin.source) == "github":
        return f"https://github.com/{plugin.source}"
    return plugin.source.removeprefix("git+")


def _plugin_install_remediation(client: str, plugin: PluginSpec) -> str:
    pkg = plugin.name
    if client == "agy":
        return f"agy plugin install {_agy_source(plugin)}"
    source = _marketplace_source(plugin)
    if client == "claude":
        return f"claude plugin marketplace add {source} && claude plugin install {pkg}@{pkg} --scope user"
    return f"codex plugin marketplace add {source} && codex plugin add {pkg}@{pkg}"


def _plugin_upgrade_remediation(client: str, plugin: PluginSpec) -> str:
    pkg = plugin.name
    if client == "agy":
        return f"agy plugin uninstall {pkg} && agy plugin install {_agy_source(plugin)}"
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
    specs: tuple[PluginSpec, ...],
) -> None:
    for spec in specs:
        pkg = spec.name
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
                    remediation=_plugin_install_remediation(client, spec),
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
        if spec.cli is not None:
            cli_ver = cli_versions.get(spec.cli)
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
                        remediation=_plugin_upgrade_remediation(client, spec),
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
    config = load_host_config(effective_env)
    findings: list[Finding] = []

    clis: dict[str, dict[str, Any]] = {}
    for spec in config.tools:
        clis[spec.name] = _inspect_cli(
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

        specs = config.plugins_for(client)
        if client == "codex":
            plugins, err = _inspect_codex_plugins(home, findings, specs)
        elif client == "agy":
            plugins, err = _inspect_agy_plugins(path, effective_env, findings, specs)
        else:
            plugins, err = _inspect_claude_plugins(path, effective_env, findings, specs)

        if err is None:
            _evaluate_client_plugins(client, plugins, cli_versions, findings, specs)

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
        "config": config.to_dict(),
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


def _uv_upgrade_command(spec: CliSpec) -> list[str]:
    command = ["uv", "tool", "install", "--upgrade"]
    if _classify_source(spec.target) == "local":
        # A local checkout can change without a version bump, so force a rebuild.
        command.append("--reinstall")
    return [*command, spec.target]


def _distribution_name(spec: CliSpec) -> str:
    """Return the name ``pipx upgrade`` knows an installed tool by."""

    match = _PACKAGE_TARGET_RE.match(spec.target)
    return match["dist"] if match else spec.name


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
    config = load_host_config(effective_env)

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
    if include_agcoord and broker != "skip" and broker_state.get("reason") == "timeout" and not force_broker:
        raise GardeniaError(
            "Refusing to mutate the AGCoord broker because the 'agc --json list --limit 1' probe timed out, "
            "so its enforcement mode ('cgroup-v2' vs 'admission-only') could not be determined safely. "
            "Retry when host load subsides, pass '--broker skip', or pass '--force-broker' to override.",
            exit_code=2,
        )
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
    target_specs = [
        spec for spec in config.tools if _tool_applies(spec, is_linux=is_linux, with_agcoord=with_agcoord)
    ]
    needs_installer = any(
        (not state["clis"][spec.name]["installed"])
        or state["clis"][spec.name]["status"] != "ok"
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
        cli_info = state["clis"][spec.name]
        installed = bool(cli_info["installed"])
        status = cli_info["status"]
        if not installed:
            cmd = (
                ["uv", "tool", "install", spec.target]
                if chosen_installer == "uv"
                else ["pipx", "install", spec.target]
            )
            actions.append(
                PlannedAction(
                    kind="command",
                    component=spec.name,
                    description=f"Install {spec.name} CLI via {chosen_installer}.",
                    command=cmd,
                )
            )
            if spec.name == "agcoord":
                agcoord_cli_mutated = True
        elif upgrade or status != "ok":
            cmd = (
                _uv_upgrade_command(spec)
                if chosen_installer == "uv"
                else ["pipx", "upgrade", _distribution_name(spec)]
            )
            actions.append(
                PlannedAction(
                    kind="command",
                    component=spec.name,
                    description=f"Upgrade {spec.name} CLI via {chosen_installer}.",
                    command=cmd,
                )
            )
            if spec.name == "agcoord":
                agcoord_cli_mutated = True
        else:
            actions.append(
                PlannedAction(
                    kind="skip",
                    component=spec.name,
                    description=f"{spec.name} {cli_info['version']} already satisfies >= {spec.min_version}.",
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
        for plugin in config.plugins_for(client):
            pkg = plugin.name
            pinfo = client_plugins.get(pkg, {})
            installed = bool(pinfo.get("installed"))
            enabled = bool(pinfo.get("enabled"))
            plugin_ver = pinfo.get("version")
            cli_ver = cli_versions.get(plugin.cli) if plugin.cli is not None else None
            drifted = bool(cli_ver and plugin_ver and cli_ver != plugin_ver)
            needs_refresh = upgrade or drifted

            if client == "agy":
                url = _agy_source(plugin)
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
                source = _marketplace_source(plugin)
                if not installed:
                    actions.append(
                        PlannedAction(
                            kind="command",
                            component=f"claude:{pkg}",
                            description=f"Register Claude Code marketplace '{source}'.",
                            command=["claude", "plugin", "marketplace", "add", source],
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
                source = _marketplace_source(plugin)
                if not installed:
                    if not marketplace_present:
                        actions.append(
                            PlannedAction(
                                kind="command",
                                component=f"codex:{pkg}",
                                description=f"Register Codex marketplace '{source}'.",
                                command=["codex", "plugin", "marketplace", "add", source],
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
                                description=f"Register Codex marketplace '{source}'.",
                                command=["codex", "plugin", "marketplace", "add", source],
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


@dataclass(frozen=True)
class SnippetResolution:
    """Outcome of locating one tool's static ``AGENTS.snippet.md`` on this host."""

    name: str
    body: str | None = None
    path: str | None = None
    origin: str | None = None
    installed: bool = False
    error: str | None = None


class HostInventory:
    """Lazily inspected, cached host state shared across repository verifications."""

    def __init__(self, env: Mapping[str, str] | None = None, *, platform: str | None = None) -> None:
        self.env = _effective_env(env)
        self.platform = sys.platform if platform is None else platform
        self.config = load_host_config(self.env)
        self._clients: dict[str, tuple[dict[str, dict[str, Any]], str | None, list[Finding]]] = {}
        self._clis: dict[tuple[str, bool], tuple[dict[str, Any], list[Finding]]] = {}

    def client_plugins(self, client: str) -> tuple[dict[str, dict[str, Any]], str | None, list[Finding]]:
        """Return ``(plugins, error, findings)`` for one client without running tool binaries."""

        if client not in self._clients:
            findings: list[Finding] = []
            specs = self.config.plugins_for(client)
            path = _which(client, self.env)
            if client == "codex":
                plugins, error = _inspect_codex_plugins(_effective_home(self.env), findings, specs)
            elif path is None:
                plugins, error = {}, "not-on-path"
            elif client == "agy":
                plugins, error = _inspect_agy_plugins(path, self.env, findings, specs)
            else:
                plugins, error = _inspect_claude_plugins(path, self.env, findings, specs)
            self._clients[client] = (plugins, error, findings)
        return self._clients[client]

    def cli(self, spec: CliSpec, *, with_agcoord: bool = False) -> tuple[dict[str, Any], list[Finding]]:
        key = (spec.name, with_agcoord)
        if key not in self._clis:
            findings: list[Finding] = []
            info = _inspect_cli(
                spec,
                is_linux=self.platform.startswith("linux"),
                with_agcoord=with_agcoord,
                env=self.env,
                findings=findings,
            )
            self._clis[key] = (info, findings)
        return self._clis[key]

    def resolve_snippet(self, name: str) -> SnippetResolution:
        """Locate ``<plugin_root>/AGENTS.snippet.md`` for a configured plugin.

        Installed plugin roots win (highest version, then ``codex``, ``claude``, ``agy``);
        a local-directory plugin source is the fallback.
        """

        plugin = self.config.plugin(name)
        if plugin is None:
            return SnippetResolution(name=name, error=f"no plugin named '{name}' is configured in {self.config.path}")

        installed = False
        problems: list[str] = []
        candidates: list[tuple[tuple[int, int, int, int, str], int, str, Path]] = []
        for index, client in enumerate(SUPPORTED_CLIENTS):
            if client not in plugin.clients:
                continue
            plugins, error, _ = self.client_plugins(client)
            if error is not None and error != "not-on-path":
                problems.append(f"{client} plugin inspection failed: {error}")
            info = plugins.get(name)
            if not info or not info.get("installed"):
                continue
            installed = True
            if not info.get("install_path"):
                continue
            snippet = Path(info["install_path"]) / PLUGIN_SNIPPET_FILENAME
            if snippet.is_file():
                rank = parse_version(info.get("version")) or (-1, -1, -1, 0, "")
                candidates.append((rank, -index, client, snippet))

        origin: str | None = None
        path: Path | None = None
        if candidates:
            _, _, origin, path = max(candidates, key=lambda item: (item[0], item[1]))
        elif _classify_source(plugin.source) == "local":
            local = Path(plugin.source) / PLUGIN_SNIPPET_FILENAME
            if local.is_file():
                origin, path = "local-source", local

        if path is None:
            reason = (
                f"installed plugin '{name}' ships no {PLUGIN_SNIPPET_FILENAME}"
                if installed
                else f"plugin '{name}' is not installed for any of: {', '.join(plugin.clients)}"
            )
            return SnippetResolution(name=name, installed=installed, error="; ".join([reason, *problems]))
        try:
            body = normalize_snippet(name, path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError) as exc:
            return SnippetResolution(name=name, path=str(path), installed=installed, error=str(exc))
        except GardeniaError as exc:
            return SnippetResolution(name=name, path=str(path), installed=installed, error=exc.message)
        return SnippetResolution(name=name, body=body, path=str(path), origin=origin, installed=installed)


def resolve_tool_snippets(
    names: tuple[str, ...] | list[str],
    *,
    env: Mapping[str, str] | None = None,
) -> dict[str, SnippetResolution]:
    """Resolve the ``AGENTS.snippet.md`` for each named tool from this host."""

    inventory = HostInventory(env)
    return {name: inventory.resolve_snippet(name) for name in names}


def verify_repo_tools(
    tools: list[str] | tuple[str, ...],
    inventory: HostInventory,
    *,
    repo_root: Path,
    stealth: bool = False,
    clients: list[str] | None = None,
    with_agcoord: bool = False,
) -> list[Finding]:
    """Check host readiness and sibling block sync for the tools a repository declares.

    Host findings are worded identically to ``check_host`` so combined reports can
    deduplicate them. Stealth repositories skip the tracked-block comparison.
    """

    findings: list[Finding] = []
    config = inventory.config
    detected = [name for name in SUPPORTED_CLIENTS if _which(name, inventory.env) is not None]
    agents_path = repo_root / "AGENTS.md"
    agents_text = agents_path.read_text(encoding="utf-8") if agents_path.is_file() else ""

    for name in tools:
        plugin = config.plugin(name)
        spec = config.tool(plugin.cli if plugin is not None and plugin.cli else name)
        if plugin is None and spec is None:
            findings.append(
                Finding(
                    scope="host",
                    severity="error",
                    component=name,
                    message=f"Repository declares tool '{name}', but it is not configured on this host.",
                    remediation=f"Add [[tools]] / [[plugins]] entries for '{name}' to {config.path}",
                )
            )
            continue

        cli_version: str | None = None
        if spec is not None:
            info, cli_findings = inventory.cli(spec, with_agcoord=with_agcoord)
            cli_version = info.get("version")
            findings.extend(cli_findings)

        if plugin is None:
            findings.append(
                Finding(
                    scope="host",
                    severity="warning",
                    component=name,
                    message=(
                        f"Tool '{name}' has no [[plugins]] entry on this host, so its AGENTS.md block "
                        "cannot be verified."
                    ),
                    remediation=f"Add a [[plugins]] entry for '{name}' to {config.path}",
                )
            )
            continue

        requested = detected if clients is None else clients
        target_clients = [client for client in requested if client in plugin.clients]
        # An explicit --clients selection that excludes the plugin is intentional.
        if not target_clients and clients is None:
            findings.append(
                Finding(
                    scope="host",
                    severity="warning",
                    component=name,
                    message=f"No target client is available to verify plugin '{name}' installation.",
                    remediation=f"Install one of: {', '.join(plugin.clients)}",
                )
            )
        for client in target_clients:
            if client not in detected:
                findings.append(
                    Finding(
                        scope="host",
                        severity="error",
                        component=client,
                        message=f"Requested agent client '{client}' was not found on PATH.",
                        remediation=f"Install '{client}' or omit it from --clients.",
                    )
                )
                continue
            plugins, error, client_findings = inventory.client_plugins(client)
            findings.extend(client_findings)
            if error is None:
                versions = {spec.name: cli_version} if spec is not None else {}
                _evaluate_client_plugins(client, plugins, versions, findings, (plugin,))

        if stealth:
            continue
        resolution = inventory.resolve_snippet(name)
        if resolution.body is None:
            # A missing installation is already reported per client above.
            if resolution.installed or resolution.path is not None:
                findings.append(
                    Finding(
                        scope="host",
                        severity="error",
                        component=name,
                        message=f"Could not verify the '{name}' AGENTS.md block: {resolution.error}.",
                        remediation=f"Reinstall or upgrade plugin '{name}' so it ships {PLUGIN_SNIPPET_FILENAME}",
                    )
                )
            continue
        block, _ = extract_sibling_block(agents_text, name)
        if block is not None and block != render_sibling_block(name, resolution.body):
            findings.append(
                Finding(
                    scope="repo",
                    severity="warning",
                    component=f"agents-md:{name}",
                    message=f"AGENTS.md '{name}' block has drifted from the installed {PLUGIN_SNIPPET_FILENAME}.",
                    remediation="gardenia repo init",
                )
            )

    unique: list[Finding] = []
    for finding in findings:
        if finding not in unique:
            unique.append(finding)
    return unique
