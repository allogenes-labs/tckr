"""Per-platform adapters for the tckr agent toolkit.

Each submodule wraps the platform-agnostic `core.TOOLS` registry into the
shape that platform expects. Import the adapter you need (each has its own
optional dependency declared in pyproject.toml under
`[project.optional-dependencies]`).
"""
