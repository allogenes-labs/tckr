"""Allow `python -m tckr <command>` as an alternative to the
`tckr` script entry point installed by pyproject.toml.
"""
from tckr.cli import main

raise SystemExit(main())
