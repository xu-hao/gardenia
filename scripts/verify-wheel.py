"""Verify built gardenia distributions at the packaging boundary."""

from __future__ import annotations

from importlib.metadata import version
import os
from pathlib import Path
import subprocess
import sys
import tarfile
import tempfile
import zipfile

expected = version("gardenia")
wheel = Path("dist") / f"gardenia-{expected}-py3-none-any.whl"
sdist = Path("dist") / f"gardenia-{expected}.tar.gz"

assert wheel.is_file(), f"Missing wheel: {wheel}"
assert sdist.is_file(), f"Missing sdist: {sdist}"

with zipfile.ZipFile(wheel) as archive:
    names = archive.namelist()
    for mod in ("__init__.py", "__main__.py", "cli.py", "host.py", "models.py", "repo.py"):
        assert f"gardenia/{mod}" in names, f"Missing module in wheel: {mod}"
    assert not any(name.startswith("tests/") for name in names), "tests/ must not be inside wheel"
    assert not any(name.startswith("scripts/") for name in names), "scripts/ must not be inside wheel"
    assert not any(name.startswith("docs/") for name in names), "docs/ must not be inside wheel"

    metadata = archive.read(f"gardenia-{expected}.dist-info/METADATA").decode("utf-8")
    assert f"Name: gardenia\n" in metadata
    assert f"Version: {expected}\n" in metadata
    runtime_deps = [
        line
        for line in metadata.splitlines()
        if line.startswith("Requires-Dist:") and "; extra ==" not in line
    ]
    assert runtime_deps == [], f"gardenia must have zero runtime dependencies: {runtime_deps}"

    entrypoints = archive.read(f"gardenia-{expected}.dist-info/entry_points.txt").decode("utf-8")
    assert "gardenia = gardenia.cli:main" in entrypoints
    script_lines = [line.strip() for line in entrypoints.splitlines() if "=" in line]
    assert script_lines == ["gardenia = gardenia.cli:main"], f"Unexpected console scripts: {script_lines}"

with tarfile.open(sdist) as archive:
    sdist_names = set(archive.getnames())
    for required in (
        f"gardenia-{expected}/README.md",
        f"gardenia-{expected}/LICENSE",
        f"gardenia-{expected}/CHANGELOG.md",
        f"gardenia-{expected}/AGENTS.md",
        f"gardenia-{expected}/gardenia.toml",
        f"gardenia-{expected}/pyproject.toml",
        f"gardenia-{expected}/docs/assets/gardenia.svg",
        f"gardenia-{expected}/tests/conftest.py",
        f"gardenia-{expected}/src/gardenia/__init__.py",
    ):
        assert required in sdist_names, f"Missing from sdist: {required}"

# Install the built wheel into a clean temporary virtual environment outside the checkout.
with tempfile.TemporaryDirectory(prefix="gardenia-wheel-") as tmpdir:
    root = Path(tmpdir)
    env = os.environ.copy()
    env.pop("PYTHONPATH", None)
    subprocess.run([sys.executable, "-m", "venv", str(root / "venv")], check=True)
    venv_python = root / "venv" / "bin" / "python"
    subprocess.run(
        [str(venv_python), "-m", "pip", "install", "--quiet", "--no-deps", str(wheel.resolve())],
        cwd=root,
        env=env,
        check=True,
    )
    installed_ver = subprocess.check_output(
        [str(venv_python), "-c", "import gardenia; print(gardenia.__version__)"],
        cwd=root,
        env=env,
        text=True,
    ).strip()
    assert installed_ver == expected

    gardenia_bin = root / "venv" / "bin" / "gardenia"
    cli_version = subprocess.check_output(
        [str(gardenia_bin), "--version"],
        cwd=root,
        env=env,
        text=True,
    ).strip()
    assert cli_version == f"gardenia {expected}"

    rendered = subprocess.check_output(
        [str(gardenia_bin), "repo", "render", "--gate", "pytest -q", "--cpu", "2", "--memory-gb", "2"],
        cwd=root,
        env=env,
        text=True,
    )
    assert "<!-- gardenia:begin -->" in rendered
    assert "<!-- gardenia:end -->" in rendered
    assert "--resource cpu=2 --resource memory=2147483648 -- pytest -q" in rendered

print(f"Verified gardenia {expected} sdist and clean installed wheel")
