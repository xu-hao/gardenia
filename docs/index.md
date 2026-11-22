# gardenia

```{image} assets/gardenia.svg
:alt: gardenia fractal mark
:width: 160px
:align: center
```

`gardenia` is the top-level bootstrapper, repository initializer, verifier, and
canonical full-stack guide for the four-tool coding-agent ecosystem — where the grain
(`agrein`), gourd (`agcoord`), root (`agroute`), and `howhard` grow together.

Every subcommand, JSON report, and guide in `gardenia` draws a hard boundary between
**per-host** workstation setup/state (`gardenia host`) and **per-repository** tracked
policy and local `.git/` state (`gardenia repo`).

## Getting started

- [Quickstart](quickstart.md) — Step 1 once per workstation (`gardenia host setup` &
  `gardenia host check`) and Step 2 once per repository (`gardenia repo init` &
  `gardenia repo check`).
- [Host vs. repository boundary](host-vs-repo.md) — complete side-by-side matrix of
  what belongs on the workstation, what is tracked in Git, and what stays local under
  `.git/`.

```{toctree}
:hidden:
:caption: Getting started

quickstart
host-vs-repo
```

## Architecture & Reference

- [Full-stack architecture](architecture.md) — end-to-end Coordinator vs. Leaf Worker
  runtime lifecycle across `agrein`, `agroute`, `howhard`, and `agcoord`.
- [CLI and schema reference](reference.md) — exact subcommands, flags, exit codes,
  `gardenia.toml` / `[tool.gardenia]` schema, and the `gardenia-check-v1` JSON report.

```{toctree}
:hidden:
:caption: Architecture & Reference

architecture
reference
```

## Project

- [Contributor workflow](contributing.md) — issues, isolated worktrees, coordinated
  checks, and atomic landing.
- [Packaging and release](releasing.md) — versioning, wheel verification, and release
  guardrails.
- [Changelog](changelog.md) — release history.
- [Image provenance](assets/README.md) — mathematical construction and checksum of
  `gardenia.svg`.
- [Session handoff](session_handoff_format.md) — four-part reporting format for changed
  work.

```{toctree}
:hidden:
:caption: Project

contributing
releasing
changelog
assets/README
session_handoff_format
```
