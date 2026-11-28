# gardenia

<img src="docs/assets/gardenia.svg" alt="gardenia fractal mark" width="160" height="160">


[Read the documentation](https://gardenia.readthedocs.io/en/latest/)

- A digital artifact factory in a box.
- Standardized workflow that addresses common agentic coding pitfalls.
- Agent-agnostic and supports routing between different agents (`Claude Code`, `Codex`, `Antigravity`).
- Automatically learns and picks the agent, model, and effort level for tasks.
- Manages host and remote resources for CPU- and memory-intensive jobs.
- Merge queue for landing concurrent pull requests.

You run `gardenia` to set up your machine and repositories. `gardenia` installs four tools **for your coding agents** to use on their own:

- [**`agrein`**](https://github.com/xu-hao/agrein): Workflow skills for agents (issue tracking, isolated git worktrees, bug reproduction, epic queues, and owner escalations).
- [**`agroute`**](https://github.com/xu-hao/agroute): Checks live usage limits and routes tasks across `Claude Code`, `Codex`, and `Antigravity`.
- [**`howhard`**](https://github.com/xu-hao/howhard): Picks the model and effort level for each task from past results and repo memory.
- [**`agcoord`**](https://github.com/xu-hao/agcoord): Schedules CPU- and memory-heavy jobs (locally or on remote runners) and runs the merge queue.

See the [documentation](https://gardenia.readthedocs.io/en/latest/) (or [docs/index.md](docs/index.md) and [docs/architecture.md](docs/architecture.md)) for details on how the agents coordinate.

## What you run

- **`gardenia host`**: Sets up or checks workstation tools, the Linux `agc` broker, user-scoped plugins across `Claude Code`, `Codex`, and `Antigravity`, and host quota reserves.
- **`gardenia repo`**: Initializes or checks a repository's `AGENTS.md` guardrail block, `CLAUDE.md -> AGENTS.md` symlink, and `gardenia.toml`.
- **`gardenia repos`**: Manages your workstation repository list, runs read-only checks across all registered repos, and runs `/var/tmp` worktree upgrade sweeps.
- **`gardenia check`**: Runs `host check` and `repo check` together, tagging each finding with `"scope": "host"` or `"scope": "repo"`.
- **`agr tui`**: Live terminal UI for model routing and subscription usage limits.
- **`agc tui`**: Live terminal UI for the machine job queue, CPU/memory/`tmpfs` usage, and the merge queue.

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

Inside your repository's primary checkout, insert the compact delimited
`<!-- gardenia:begin --> ... <!-- gardenia:end -->` guardrail block into `AGENTS.md`,
followed by one block per active plugin stamped from its `AGENTS.snippet.md` (preserving
every surrounding project-specific line), link `CLAUDE.md -> AGENTS.md`, and persist your
profile to `gardenia.toml`:

```sh
gardenia repo init --gate "bash scripts/check" --cpu 4 --memory-gb 4 --save-config
gardenia repo check
```

### Step 3 — Across your managed repositories (`gardenia repos`)

Register existing primary checkouts once (new `gardenia repo init` runs register primary
checkouts automatically while skipping linked worktrees), verify every repository in one
command, and run isolated-worktree policy or dependency upgrade sweeps:

```sh
gardenia repos scan --root ~/git
gardenia repos list
gardenia repos check --strict
gardenia repos upgrade --dry-run
gardenia repos upgrade --package howhard --file-issues
```

### Unified diagnostic

```sh
gardenia check --json
```

### Live monitoring

```sh
agr tui   # realtime model-route and subscription quota view
agc tui   # realtime machine job queue, resource usage, and merge queue
```

## Documentation

- [Quickstart](docs/quickstart.md) — step-by-step workstation setup and repository onboarding.
- [Host vs. repository boundary](docs/host-vs-repo.md) — complete side-by-side matrix of host state, tracked repo policy, and local `.git/` state.
- [Full-stack architecture](docs/architecture.md) — end-to-end Coordinator vs. Leaf Worker runtime lifecycle.
- [CLI and schema reference](docs/reference.md) — subcommands, flags, exit codes, `gardenia.toml`, and `gardenia-check-v1` JSON schema.
- [Contributor workflow](docs/contributing.md) and [Packaging and release](docs/releasing.md).

Licensed under the [MIT License](LICENSE).
