"""Run the daemon as a background service, on whichever service manager the
host actually has.

Two backends, both real: **systemd** user units (Linux/WSL2) and **launchd**
LaunchAgents (macOS). Anything else answers honestly — :class:`NoScheduler`
reports what is missing and points at ``claude-chat serve``; it never pretends
to have installed something.

Override the pick with ``CLAUDE_CHAT_SCHEDULER=systemd|launchd`` (the tests'
knob).
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from typing import Any

UNIT_NAME = "claude-chat-rail"


class SchedulerError(RuntimeError):
    """A service operation failed — the message carries the CLI's own words."""


def run_cmd(argv: list[str], timeout: int = 30) -> tuple[int, str]:
    """Run a service CLI (systemctl/launchctl); (rc, output). Never raises —
    an absent binary reports as rc 1 with the reason."""
    try:
        proc = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError) as exc:
        return 1, f"{argv[0]} unavailable: {exc}"
    return proc.returncode, (proc.stdout + proc.stderr).strip()


class SystemdScheduler:
    name = "systemd"

    def __init__(self, unit_dir: Path | None = None):
        self.unit_dir = unit_dir or Path.home() / ".config" / "systemd" / "user"

    def _ctl(self, *args: str) -> tuple[int, str]:
        return run_cmd(["systemctl", "--user", *args])

    def available(self) -> tuple[bool, str]:
        rc, out = self._ctl("is-system-running")
        # degraded/running both mean the user manager answers; only a missing
        # binary or no user session is a real no.
        if "unavailable" in out or "Failed to connect" in out:
            return False, out
        return True, ""

    def install_service(self, *, description: str, workdir: Path,
                        env: dict[str, str], argv: list[str]) -> dict[str, Any]:
        self.unit_dir.mkdir(parents=True, exist_ok=True)
        env_lines = "\n".join(f'Environment="{k}={v}"' for k, v in sorted(env.items()))
        (self.unit_dir / f"{UNIT_NAME}.service").write_text(f"""[Unit]
Description={description}
After=network.target

[Service]
Restart=on-failure
RestartSec=5
WorkingDirectory={workdir}
{env_lines}
ExecStart={" ".join(argv)}

[Install]
WantedBy=default.target
""")
        for step in (["daemon-reload"], ["enable", "--now", f"{UNIT_NAME}.service"]):
            rc, out = self._ctl(*step)
            if rc != 0:
                raise SchedulerError(f"systemctl {' '.join(step)}: {out}")
        return {"unit": f"{UNIT_NAME}.service", "unit_dir": str(self.unit_dir)}

    def remove(self) -> dict[str, Any]:
        unit = f"{UNIT_NAME}.service"
        path = self.unit_dir / unit
        if not path.exists():
            return {"removed": []}
        self._ctl("disable", "--now", unit)
        path.unlink(missing_ok=True)
        self._ctl("daemon-reload")
        # A unit that ever failed stays in the runtime as `not-found failed`
        # after its file is gone — permanent ghost noise.
        self._ctl("reset-failed", unit)
        return {"removed": [unit]}

    def status(self) -> dict[str, Any]:
        unit = f"{UNIT_NAME}.service"
        if not (self.unit_dir / unit).exists():
            return {"installed": False, "state": "absent", "failed": False}
        _, state = self._ctl("is-active", unit)
        rc_failed, _ = self._ctl("is-failed", unit)
        return {"installed": True, "state": state or "unknown",
                "failed": rc_failed == 0}

    def restart(self) -> tuple[bool, str]:
        rc, out = self._ctl("restart", f"{UNIT_NAME}.service")
        return rc == 0, out


class LaunchdScheduler:
    name = "launchd"

    def __init__(self, unit_dir: Path | None = None):
        self.unit_dir = unit_dir or Path.home() / "Library" / "LaunchAgents"
        self.label = f"local.{UNIT_NAME}"

    @property
    def _plist(self) -> Path:
        return self.unit_dir / f"{self.label}.plist"

    def available(self) -> tuple[bool, str]:
        rc, out = run_cmd(["launchctl", "print-disabled", "user/%d" % os.getuid()])
        return (True, "") if rc == 0 else (False, out)

    def install_service(self, *, description: str, workdir: Path,
                        env: dict[str, str], argv: list[str]) -> dict[str, Any]:
        self.unit_dir.mkdir(parents=True, exist_ok=True)
        args = "".join(f"    <string>{a}</string>\n" for a in argv)
        envs = "".join(f"    <key>{k}</key><string>{v}</string>\n"
                       for k, v in sorted(env.items()))
        self._plist.write_text(f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>{self.label}</string>
  <key>ProgramArguments</key>
  <array>
{args}  </array>
  <key>EnvironmentVariables</key>
  <dict>
{envs}  </dict>
  <key>WorkingDirectory</key><string>{workdir}</string>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>StandardErrorPath</key><string>/tmp/{UNIT_NAME}.err</string>
  <key>StandardOutPath</key><string>/tmp/{UNIT_NAME}.out</string>
</dict>
</plist>
""")
        run_cmd(["launchctl", "unload", str(self._plist)])   # idempotent reinstall
        rc, out = run_cmd(["launchctl", "load", "-w", str(self._plist)])
        if rc != 0:
            raise SchedulerError(f"launchctl load: {out}")
        return {"unit": self.label, "unit_dir": str(self.unit_dir)}

    def remove(self) -> dict[str, Any]:
        if not self._plist.exists():
            return {"removed": []}
        run_cmd(["launchctl", "unload", "-w", str(self._plist)])
        self._plist.unlink(missing_ok=True)
        return {"removed": [self.label]}

    def status(self) -> dict[str, Any]:
        if not self._plist.exists():
            return {"installed": False, "state": "absent", "failed": False}
        rc, out = run_cmd(["launchctl", "list", self.label])
        return {"installed": True,
                "state": "active" if rc == 0 else "inactive",
                "failed": '"LastExitStatus" = 0' not in out and rc == 0}

    def restart(self) -> tuple[bool, str]:
        run_cmd(["launchctl", "unload", str(self._plist)])
        rc, out = run_cmd(["launchctl", "load", str(self._plist)])
        return rc == 0, out


class NoScheduler:
    """The honest answer on a platform without a supported service manager."""

    name = "none"

    def available(self) -> tuple[bool, str]:
        return False, (f"no supported service manager on {sys.platform} — "
                       "run `claude-chat serve` in a terminal, or wrap it in "
                       "whatever supervisor you use")

    def install_service(self, **_: Any) -> dict[str, Any]:
        raise SchedulerError(self.available()[1])

    def remove(self) -> dict[str, Any]:
        return {"removed": []}

    def status(self) -> dict[str, Any]:
        return {"installed": False, "state": "unsupported", "failed": False}

    def restart(self) -> tuple[bool, str]:
        return False, self.available()[1]


def resolve_scheduler(unit_dir: Path | None = None):
    forced = (os.environ.get("CLAUDE_CHAT_SCHEDULER") or "").strip().lower()
    if forced == "systemd" or (not forced and sys.platform.startswith("linux")):
        return SystemdScheduler(unit_dir=unit_dir)
    if forced == "launchd" or (not forced and sys.platform == "darwin"):
        return LaunchdScheduler(unit_dir=unit_dir)
    if forced == "none":
        return NoScheduler()
    return NoScheduler()
