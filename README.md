# gardenia

<img src="docs/assets/gardenia.svg" alt="gardenia rosette mark" width="160" height="160">

*Where the grain (`agrein`), gourd (`agcoord`), root (`agroute`), and `howhard` grow together.*

[Read the documentation](https://gardenia.readthedocs.io/en/latest/).

`gardenia` is the zero-dependency bootstrapper, repository initializer, verifier, and
canonical full-stack guide for the four-tool coding-agent ecosystem:

| Layer | Role | Scope |
| --- | --- | --- |
| [**agrein**](https://github.com/xu-hao/agrein) (*“a grain”*) | Contributor & epic/subagent skills (`ticket-delivery`, `epic-queue`, `bug-investigation`, `owner-escalation`) | Host plugin + repo policy trigger |
| [**agcoord**](https://github.com/xu-hao/agcoord) (`agc`, *“a gourd”*) | Linux local resource/cgroup-v2 broker and atomic `agc land` merge gate | Host CLI + broker + plugin; repo check/land command |
| [**howhard**](https://github.com/xu-hao/howhard) | Bayesian model/effort selector, outcome accounting, and writable semantic memory (`>= 4.0.0`) | Host CLI + plugin; per-clone `.git/howhard` store |
| [**agroute**](https://github.com/xu-hao/agroute) (`agr`, *“a root”*) | Cross-agent (`codex`, `claude`, `agy`) live quota filtering and execution-plan resolution (`>= 0.6.0`) | Host CLI + plugin + `~/.config/agroute/config.toml`; per-clone `.git/agroute` store |

`gardenia` enforces an explicit boundary between **per-host** workstation setup and
**per-repository** policy and local `.git/` state across every CLI subcommand, JSON
schema, and documentation page:

- **`gardenia host ...`** inspects or configures only workstation / OS-user state
  (`PATH` CLIs, Linux `agc` broker, user-scoped plugins across Codex/Claude Code/Antigravity,
  and `~/.config/agroute/config.toml`). It never inspects or mutates a Git repository.
- **`gardenia repo ...`** inspects, renders, or updates only a target Git repository,
  distinguishing **tracked repository settings** (`AGENTS.md` managed block,
  `CLAUDE.md -> AGENTS.md` symlink, `gardenia.toml` / `[tool.gardenia]`, optional
  `howhard.toml` and `agroute.toml`) from **local per-clone `.git/` state**
  (`.git/howhard` and `.git/agroute`). It never mutates user-home host installations.
- **`gardenia check`** combines `host check` and (when inside a Git checkout or given
  `--repo`) `repo check`, tagging every finding with `"scope": "host"` or
  `"scope": "repo"` and a concrete remediation command.

## Quickstart

Install `gardenia` (Python 3.11+, zero third-party runtime dependencies):

```sh
uv tool install gardenia
gardenia --version
```

### Step 1 — Once per workstation (`gardenia host`)

Preview and apply host CLIs, safe Linux broker setup (preserving any existing
`cgroup-v2` enforced broker), user-scoped plugins across detected clients, and optional
quota reserves:

```sh
gardenia host setup --dry-run
gardenia host setup --reserve-pct 5 --host-reserve-credit 20 --min-remaining-credit 5
gardenia host check
```

### Step 2 — Once per repository (`gardenia repo`)

Inside your repository's primary checkout, insert the delimited
`<!-- gardenia:begin --> ... <!-- gardenia:end -->` guardrail block into `AGENTS.md`
(preserving every surrounding project-specific line), link `CLAUDE.md -> AGENTS.md`,
and persist your profile to `gardenia.toml`:

```sh
gardenia repo init --gate "bash scripts/check" --cpu 4 --memory-gb 4 --save-config
gardenia repo check
```

### Unified diagnostic

```sh
gardenia check --json
```

## Documentation

- [Quickstart](docs/quickstart.md) — step-by-step workstation setup and repository onboarding.
- [Host vs. repository boundary](docs/host-vs-repo.md) — complete side-by-side matrix of host state, tracked repo policy, and local `.git/` state.
- [Full-stack architecture](docs/architecture.md) — end-to-end Coordinator vs. Leaf Worker runtime lifecycle.
- [CLI and schema reference](docs/reference.md) — subcommands, flags, exit codes, `gardenia.toml`, and `gardenia-check-v1` JSON schema.
- [Contributor workflow](docs/contributing.md) and [Packaging and release](docs/releasing.md).

Licensed under the [MIT License](LICENSE).
