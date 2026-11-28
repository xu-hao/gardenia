from importlib.metadata import version as distribution_version

project = "gardenia"
release = distribution_version("gardenia")
version = release
extensions = ["myst_parser"]
source_suffix = {".md": "markdown"}
master_doc = "index"
html_theme = "furo"
exclude_patterns = ["_build"]
myst_heading_anchors = 3
