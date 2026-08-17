"""ping-chat-hub machine configuration — hub.toml.

Two config files, two jobs. THIS one carries install shape: paths, ports,
binaries, which terminal adapter spawns tabs. `hub/settings.json` keeps the
preferences the UI toggles at runtime (spawn model, effort, voice) and is not
touched here.

Every key resolves in one order:

    hub.toml value  ->  derivation from a platform/base root  ->  product default

No user-specific literal appears in this file (G0 amendment 1, heron
2026-08-17): this machine reproduces its current behaviour by DERIVATION —
`gated_doc` falls out of `base_gbl`, the WSL home falls out of asking WSL — and
that is exactly what makes the same code correct on someone else's box. The
hardcode tripwire in the test suite runs with an EMPTY whitelist.

Config file location resolves by env only (the file is what tells us where the
stores are, so it cannot be found via a key inside itself):

    $PING_HUB_CONFIG  ->  $BASE_GBL/hub.toml  ->  ~/.base-gbl/hub.toml

Anything that shells out (the WSL distro, the WSL home) is resolved LAZILY and
cached — `daemon.py` and `engine.py` build their constants at import time, and
an import must never block on `wsl.exe`.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import tomllib
from pathlib import Path
from typing import Any

# Product defaults. These are ping-chat-hub's own numbers, not anyone's
# machine: the hub serves 7799, the WSL bridge serves 7798, and the bundled
# Parakeet bridge follows the whisper.cpp convention on 8973.
HUB_PORT = 7799
BRIDGE_PORT = 7798
STT_PORT = 8973

_MISSING = object()


# ── probes: every environment read goes through here so tests can drive the
#    whole resolver hermetically, with no subprocess and no real filesystem ──
class Probe:
    """Environment access, injectable. The default implementation reads the
    real machine; tests pass a stub with the same surface."""

    def __init__(self, env: dict | None = None) -> None:
        self.env = dict(os.environ if env is None else env)

    def home(self) -> Path:
        return Path(self.env.get("USERPROFILE") or self.env.get("HOME")
                    or str(Path.home()))

    def system_root(self) -> Path:
        return Path(self.env.get("SystemRoot") or self.env.get("SYSTEMROOT")
                    or "/")

    def exists(self, p: Path) -> bool:
        try:
            return p.exists()
        except OSError:
            return False

    def which(self, name: str) -> str:
        return shutil.which(name) or ""

    def read_text(self, p: Path) -> str:
        try:
            return p.read_text(encoding="utf-8")
        except OSError:
            return ""

    def wsl_distro(self) -> str:
        """Default distro name. `wsl -l -q` prints UTF-16LE, which arrives as
        NUL-interleaved text — the same strip engine.py already does for
        `wsl hostname -I`."""
        out = self._run(["wsl.exe", "-l", "-q"])
        for line in out.splitlines():
            if line.strip():
                return line.strip()
        return ""

    def wsl_home(self) -> str:
        return self._run(["wsl.exe", "-e", "sh", "-lc", "echo $HOME"]).strip()

    @staticmethod
    def _run(cmd: list[str], timeout: int = 15) -> str:
        try:
            r = subprocess.run(cmd, capture_output=True, text=True,
                               timeout=timeout, encoding="utf-8",
                               errors="replace")
        except (OSError, subprocess.TimeoutExpired):
            return ""
        return (r.stdout or "").replace("\x00", "")


# ── path helpers ─────────────────────────────────────────────────────────────
def win_to_wsl(path: str) -> str:
    """C:/Users/<user>/f.md -> /mnt/c/Users/<user>/f.md. Anything already POSIX
    comes back unchanged, so a Mac/Linux value passes through."""
    p = str(path).replace("\\", "/")
    if len(p) > 1 and p[1] == ":":
        return f"/mnt/{p[0].lower()}{p[2:]}"
    return p


def expand(value: str, probe: Probe) -> str:
    """~ and %VAR% expansion against the probe's env, never os.environ — a
    hermetic test must be able to pin USERPROFILE."""
    if not value:
        return ""
    v = os.path.expandvars(value) if "%" in value or "$" in value else value
    if v.startswith("~"):
        v = str(probe.home()) + v[1:]
    return v


class _Section:
    """Reads `hub.toml` first, derivation second. A key present but empty in
    the file means 'derive it' — that is how the schema ships user-specific
    keys with no user-specific defaults."""

    def __init__(self, raw: dict, cfg: "Config") -> None:
        self._raw = raw or {}
        self._cfg = cfg
        self._cache: dict[str, Any] = {}

    def _get(self, key: str, default: Any = _MISSING) -> Any:
        v = self._raw.get(key)
        if v is None or v == "" or v == []:
            return None if default is _MISSING else default
        if isinstance(v, str):
            v = expand(v, self._cfg.probe)   # hand-edited ~/… and %VAR% work
        return v

    def _derived(self, key: str, fn) -> Any:
        """File value wins; otherwise derive once and cache."""
        v = self._get(key)
        if v is not None:
            return v
        if key not in self._cache:
            self._cache[key] = fn()
        return self._cache[key]


class HubSection(_Section):
    @property
    def port(self) -> int:
        return int(self._get("port", HUB_PORT))

    @property
    def bind(self) -> str:
        # 0.0.0.0, not loopback: WSL's tailscale serve proxies phone traffic in
        # via the NAT gateway IP, which cannot reach a 127.0.0.1 bind.
        return str(self._get("bind", "0.0.0.0"))

    @property
    def register_standing_title(self) -> bool:
        # False for shadow/test instances: two daemons re-binding the same
        # standing title churn its registration and double-touch its sentinel.
        return bool(self._get("register_standing_title", True))

    @property
    def standing_title(self) -> str:
        return str(self._get("standing_title", "chris"))

    @property
    def standing_session(self) -> str:
        return str(self._get("standing_session", "hub-chris-standing"))


class PathsSection(_Section):
    @property
    def base_bin(self) -> str:
        return str(self._get("base_bin", "base"))

    @property
    def base_gbl(self) -> Path:
        return Path(self._derived(
            "base_gbl", lambda: str(self._cfg.probe.home() / ".base-gbl")))

    @property
    def base_store(self) -> Path:
        """base's per-tier store — relay inboxes, sessions.json, squads, hub/."""
        return self.base_gbl / ".base"

    @property
    def claude_home(self) -> Path:
        return Path(self._derived(
            "claude_home", lambda: str(self._cfg.probe.home() / ".claude")))

    @property
    def hook_events(self) -> Path:
        return Path(self._derived(
            "hook_events",
            lambda: str(self._cfg.probe.home() / ".base" / "hook-events.jsonl")))

    @property
    def hub_home(self) -> Path:
        """Where the installer provisions bundled voice engines."""
        return Path(self._derived(
            "hub_home", lambda: str(self._cfg.probe.home() / ".ping-hub")))

    @property
    def sound_dir(self) -> Path:
        return Path(self._derived(
            "sound_dir", lambda: str(self._cfg.probe.system_root() / "Media")))

    @property
    def gated_doc(self) -> str:
        """The gated-build process doc, native path. Lives in base's global
        tier, so it derives from base_gbl rather than being spelled out."""
        return str(self._derived(
            "gated_doc",
            lambda: str(self.base_gbl / "PROCESS-gated-build.md").replace("\\", "/")))

    @property
    def gated_doc_wsl(self) -> str:
        """The same doc as the WSL side sees it (/mnt/c/...)."""
        return str(self._derived("gated_doc_wsl",
                                 lambda: win_to_wsl(self.gated_doc)))


class WslSection(_Section):
    @property
    def enabled(self) -> bool:
        """False collapses the two-sided model to one side — the Mac posture,
        and the honest posture on a Windows box with no WSL. A distro that
        cannot be resolved disables the side rather than guessing a name."""
        v = self._get("enabled")
        if v is not None:
            return bool(v)
        return bool(self.distro)

    @property
    def distro(self) -> str:
        return str(self._derived("distro", self._cfg.probe.wsl_distro))

    @property
    def home_linux(self) -> str:
        return str(self._derived("home_linux", self._cfg.probe.wsl_home))

    @property
    def unc_root(self) -> str:
        return str(self._derived(
            "unc_root",
            lambda: (rf"\\wsl.localhost\{self.distro}" if self.distro else "")))

    @property
    def home_unc(self) -> str:
        """Windows-visible path to the WSL home (\\\\wsl.localhost\\<distro>\\home\\<user>)."""
        root, home = self.unc_root, self.home_linux
        if not root or not home:
            return ""
        return root + home.replace("/", "\\")

    @property
    def base_bin(self) -> str:
        return str(self._derived(
            "base_bin",
            lambda: (f"{self.home_linux}/.local/bin/base" if self.home_linux
                     else "base")))

    @property
    def bridge_port(self) -> int:
        return int(self._get("bridge_port", BRIDGE_PORT))

    @property
    def bridge_deploy(self) -> str:
        """Linux-side dir the bridge and spawn scripts are written to, relative
        to the WSL home. An absolute value is honoured as-is."""
        v = str(self._raw.get("bridge_deploy") or "~/.local/share/hub-bridge")
        return v[2:] if v.startswith("~/") else v

    @property
    def bridge_deploy_linux(self) -> str:
        d = self.bridge_deploy
        if d.startswith("/"):
            return d
        return f"{self.home_linux}/{d}" if self.home_linux else ""

    @property
    def bridge_deploy_unc(self) -> str:
        """The same directory as Windows sees it. Both renderings come from one
        key — daemon.py wrote the spawn script over UNC and executed it by
        Linux path, and the two drifting apart is a silent broken spawn."""
        d, root = self.bridge_deploy, self.unc_root
        if d.startswith("/"):
            return (root + d.replace("/", "\\")) if root else ""
        u = self.home_unc
        return (u + "\\" + d.replace("/", "\\")) if u else ""


class TerminalSection(_Section):
    @property
    def adapter(self) -> str:
        return str(self._get("adapter", "wt"))

    @property
    def wsl_profile(self) -> str:
        """Windows Terminal profile GUID for the WSL side. Derived from cx.toml
        [switch] when cx-ptt is installed; empty means 'let WT pick its
        default', which is a working spawn, not a broken one."""
        return str(self._derived("wsl_profile", self._cx_switch_guid))

    def _cx_switch_guid(self) -> str:
        text = self._cfg.probe.read_text(self._cfg.cx_ptt.cx_toml)
        if not text:
            return ""
        in_switch = False
        for line in text.splitlines():
            s = line.strip()
            if s.startswith("["):
                in_switch = s == "[switch]"
                continue
            if in_switch and "=" in s:
                k, _, v = s.partition("=")
                v = v.strip().strip('"').strip("'")
                if v.startswith("{") and v.endswith("}"):
                    return v
        return ""

    @property
    def windows_profile(self) -> str:
        return str(self._get("windows_profile", "PowerShell"))

    @property
    def restore_focus(self) -> bool:
        """Suppress the spawned terminal instead of letting it steal focus.
        Windows-only mechanism (user32); other adapters ignore it."""
        return bool(self._get("restore_focus", True))


class SpawnSection(_Section):
    @property
    def default_model(self) -> str:
        return str(self._get("default_model", ""))

    @property
    def opus_1m(self) -> bool:
        return bool(self._get("opus_1m", True))

    @property
    def disallowed_tools(self) -> list[str]:
        return list(self._get("disallowed_tools", ["AskUserQuestion"]))

    @property
    def pre_trust(self) -> bool:
        return bool(self._get("pre_trust", True))


class SttSection(_Section):
    @property
    def enabled(self) -> bool:
        # Bundled dependency (Chris ruling 2026-08-17: the package is
        # opinionated). False is a human override, never a detection result —
        # a missing server reports "absent" through /api/capabilities instead.
        return bool(self._get("enabled", True))

    @property
    def url(self) -> str:
        return str(self._get("url", f"http://127.0.0.1:{STT_PORT}/inference"))

    @property
    def ffmpeg(self) -> str:
        return str(self._get("ffmpeg", "ffmpeg"))

    @property
    def autostart(self) -> bool:
        return bool(self._get("autostart", True))

    @property
    def launcher(self) -> list[str]:
        """Command that starts the STT server; the installer writes it."""
        v = self._get("launcher", [])
        return [str(x) for x in v] if isinstance(v, list) else [str(v)]

    @property
    def model_dir(self) -> str:
        return str(self._derived(
            "model_dir",
            lambda: str(self._cfg.paths.hub_home / "stt" / "model")))


class TtsSection(_Section):
    @property
    def enabled(self) -> bool:
        return bool(self._get("enabled", True))

    @property
    def command(self) -> list[str]:
        """Argv prefix that speaks text. Derivation order: the installer's own
        provisioned launcher, then a `say` on PATH, then an existing kokoro
        install under the user's tools dir — so a machine that already has the
        engine is adopted rather than re-provisioned."""
        v = self._get("command")
        if v:
            return [str(x) for x in v] if isinstance(v, list) else [str(v)]
        if "command" not in self._cache:
            self._cache["command"] = self._derive_command()
        return self._cache["command"]

    def _derive_command(self) -> list[str]:
        p = self._cfg.probe
        bundled = self._cfg.paths.hub_home / "tts" / "say.cmd"
        if p.exists(bundled):
            return ["cmd", "/c", str(bundled)]
        bundled_sh = self._cfg.paths.hub_home / "tts" / "say"
        if p.exists(bundled_sh):
            return [str(bundled_sh)]
        for tools in ("tools", "Tools"):
            legacy = p.home() / tools / "kokoro" / "say.cmd"
            if p.exists(legacy):
                return ["cmd", "/c", str(legacy)]
        onpath = p.which("say")
        return [onpath] if onpath else []

    @property
    def default_voice(self) -> str:
        return str(self._get("default_voice", "af_heart"))


class CxPttSection(_Section):
    @property
    def enabled(self) -> bool:
        """Optional module. Absent cx.toml means no channel badges, which is a
        capability answer, not an error."""
        v = self._get("enabled")
        if v is not None:
            return bool(v)
        return self._cfg.probe.exists(self.cx_toml)

    @property
    def cx_toml(self) -> Path:
        return Path(self._derived("cx_toml",
                                  lambda: str(self._cfg.paths.base_gbl / "cx.toml")))

    @property
    def cx_slot(self) -> Path:
        return Path(self._derived("cx_slot", self._derive_slot))

    def _derive_slot(self) -> str:
        p = self._cfg.probe
        for tools in ("Tools", "tools"):
            c = p.home() / tools / "stt" / "cx-slot.py"
            if p.exists(c):
                return str(c)
        return str(p.home() / "Tools" / "stt" / "cx-slot.py")

    @property
    def python(self) -> str:
        return str(self._get("python", "python"))


class Config:
    def __init__(self, raw: dict | None = None, probe: Probe | None = None,
                 source: Path | None = None) -> None:
        raw = raw or {}
        self.source = source
        self.probe = probe or Probe()
        self.hub = HubSection(raw.get("hub", {}), self)
        self.paths = PathsSection(raw.get("paths", {}), self)
        self.wsl = WslSection(raw.get("wsl", {}), self)
        self.terminal = TerminalSection(raw.get("terminal", {}), self)
        self.spawn = SpawnSection(raw.get("spawn", {}), self)
        self.stt = SttSection(raw.get("stt", {}), self)
        self.tts = TtsSection(raw.get("tts", {}), self)
        self.cx_ptt = CxPttSection(raw.get("cx_ptt", {}), self)


def config_path(env: dict | None = None) -> Path | None:
    """Where hub.toml lives, resolved by env only. None = no file, which is a
    supported state: every key derives or defaults."""
    e = dict(os.environ if env is None else env)
    explicit = e.get("PING_HUB_CONFIG")
    if explicit:
        return Path(explicit)
    roots = []
    if e.get("BASE_GBL"):
        roots.append(Path(e["BASE_GBL"]))
    home = e.get("USERPROFILE") or e.get("HOME")
    roots.append(Path(home) / ".base-gbl" if home else Path.home() / ".base-gbl")
    for r in roots:
        p = r / "hub.toml"
        try:
            if p.is_file():
                return p
        except OSError:
            continue
    return None


def load(path: Path | None = None, env: dict | None = None,
         probe: Probe | None = None) -> Config:
    """Parse hub.toml if there is one. A malformed file is louder than a
    missing one — it means someone edited it and got it wrong."""
    probe = probe or Probe(env)
    p = path if path is not None else config_path(env)
    raw: dict = {}
    if p is not None:
        try:
            with open(p, "rb") as fh:
                raw = tomllib.load(fh)
        except FileNotFoundError:
            p = None
        except (OSError, tomllib.TOMLDecodeError) as e:
            raise RuntimeError(f"hub.toml unreadable ({p}): {e}") from e
    return Config(raw, probe=probe, source=p)


_CFG: Config | None = None


def get() -> Config:
    """Process-wide config, loaded once."""
    global _CFG
    if _CFG is None:
        _CFG = load()
    return _CFG


def reset(cfg: Config | None = None) -> None:
    """Test seam: swap or clear the process-wide config."""
    global _CFG
    _CFG = cfg
