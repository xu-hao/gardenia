# Packaging and release

The distribution name, Python import package (`src/gardenia/`), and sole console script
name are all `gardenia`. `gardenia` has zero third-party runtime dependencies
(`dependencies = []`, Python 3.11+ standard library only).

The single version source of truth is `src/gardenia/__init__.py` (`__version__`).
`pyproject.toml` reads it dynamically via `setuptools`, `gardenia --version` prints it,
and Sphinx reads the installed distribution version.

## Release checklist

1. Bump `__version__` in `src/gardenia/__init__.py` using `MAJOR.MINOR.PATCH` and add a
   dated section in `CHANGELOG.md`. A source commit is a release candidate until
   published; a changelog entry alone does not mean a release is on PyPI.
2. Land the exact release commit through `agc land` with `bash scripts/check` as its
   gate. The gate runs the full hermetic test suite, builds wheel and sdist archives,
   verifies the wheel in a clean temporary virtual environment via
   `scripts/verify-wheel.py`, and builds Sphinx docs with `-W`.
3. Create an annotated tag `vX.Y.Z` and GitHub release only when explicitly requested.
4. Never upload a distribution to PyPI unless the owner explicitly authorizes that exact
   upload. Permission to implement, test, commit, push, merge, tag, or create a GitHub
   release does not authorize a PyPI upload.

CI workflows contain no PyPI upload credentials. The Read the Docs site builds from
`.readthedocs.yaml` with `fail_on_warning: true`.
