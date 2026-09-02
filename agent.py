"""Compatibility launcher for the native AGI Yukino runtime.

The product implementation lives under :mod:`service_club`; this module only
keeps ``python agent.py`` working for existing local shortcuts.
"""

from service_club.cli import main

if __name__ == "__main__":
    raise SystemExit(main())
