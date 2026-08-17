"""Bundled speech engines, vendored into the package (Chris ruling 2026-08-17).

These modules are COPIED into the install home by `ping-hub install` and run
there under their own venvs — they are never imported into the daemon process,
which stays stdlib-only. Importing this package therefore pulls in nothing.
"""
