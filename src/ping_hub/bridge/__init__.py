"""The WSL bridge, vendored into the package (Chris ruling 2026-08-17).

`wsl_bridge.py` is not imported here. It is a FILE the installer copies into
WSL, where it runs under WSL's own Python against WSL's own base store — the
hub never reaches across `\\\\wsl.localhost` to read that store itself. Keeping
it as package data rather than a loose repo directory is what makes it survive
a `pip install`.
"""
