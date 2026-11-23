# Contributor workflow

Track every code, test, documentation, or packaging change with a descriptive GitHub
issue (`#N — one sentence describing the work`). Develop and validate only in an
issue-specific Git worktree branched from `origin/main`, keeping the primary checkout
read-only during development.

```sh
git fetch origin
git worktree add -b issue-N-description /var/tmp/agrein-gardenia-N-wt origin/main
cd /var/tmp/agrein-gardenia-N-wt
uv venv --python 3.12 /var/tmp/agrein-gardenia-N-venv
uv pip install --python /var/tmp/agrein-gardenia-N-venv/bin/python -e '.[test,docs]'
```

Run the full validation gate through `agcoord` on Linux (using integer bytes for memory
claims):

```sh
agc run --label "<short plain-English summary>" --no-overflow --resource cpu=4 --resource memory=4294967296 -- bash scripts/check
```

`scripts/check` runs:

1. `python -m pytest -q` (hermetic behavioral tests using temporary `HOME`, `PATH`, fake
   CLI shims, and temporary Git repositories — never touching real `~/.codex`,
   `~/.claude`, `~/.gemini`, `~/.config`, or `~/.local`).
2. `python -m build` (sdist and wheel).
3. `python scripts/verify-wheel.py` (inspecting metadata, entry point, clean wheel
   contents, and executing `gardenia --version` and `gardenia repo render` from an
   isolated temporary virtual environment).
4. `python -m sphinx -W --keep-going -b html docs docs/_build/html` (strict warning-free
   Read the Docs build).

Update affected canonical documents, `gardenia.toml` / `AGENTS.md` (via
`gardenia repo init --allow-worktree` inside a ticket worktree if policy changes), and
`CHANGELOG.md` for user-facing changes.

Gate and publish pull requests atomically through one `agc land` command:

```sh
agc land <pr> --label "land #<pr> <short summary>" --no-overflow --resource cpu=4 --resource memory=4294967296 -- bash scripts/check
```

Documentation-only changes follow the narrower exemption in `AGENTS.md`. End changed
work with the four-part [session handoff](session_handoff_format.md).
