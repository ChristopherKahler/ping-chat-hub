"""hub.toml resolver — the parity proof.

The point of this suite is one claim, made mechanically rather than by
inspection: **extracting the hardcodes changed nothing.** Every literal that
used to sit in ``daemon.py`` and ``engine.py`` appears here once, on the
right-hand side of an assertion, and the resolver has to derive its way back to
it from nothing but a pinned environment.

That environment is a SYNTHETIC operator, not a real one. The values below are
invented; they are what the stub probe reports, so the parity table proves
"derives back to whatever the environment says" — which is the actual claim, and
the only one that stays true on someone else's machine. No real username,
hostname or device id belongs in this repo.

``src/ping_hub`` is separately checked by
:func:`test_no_hardcoded_operator_paths_in_src`, which fails on ANY absolute
user path, not just one person's (G0 amendment 1, heron 2026-08-17).

Hermetic: no subprocess, no real filesystem, no network. The probe is a stub.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from ping_hub import config as cfgmod
from ping_hub.config import Config, Probe


# ── a synthetic machine, invented for this suite ─────────────────────────────
OPERATOR_HOME = r"C:\Users\operator"
OPERATOR_LINUX_HOME = "/home/operator"
OPERATOR_DISTRO = "Ubuntu"
OPERATOR_WT_GUID = "{11111111-2222-3333-4444-555555555555}"

CX_TOML_PATH = f"{OPERATOR_HOME}\\.base-gbl\\cx.toml"
SAY_CMD = f"{OPERATOR_HOME}\\tools\\kokoro\\say.cmd"
CX_SLOT_PATH = f"{OPERATOR_HOME}\\Tools\\stt\\cx-slot.py"

CX_TOML_TEXT = f"""\
[switch]
wsl = "{OPERATOR_WT_GUID}"

[slots]
1 = {{ codename = "wslterm1", side = "wsl" }}
"""


class StubProbe(Probe):
    """A machine that does not exist, so the assertions can be exact."""

    def __init__(self, *, files: dict[str, str] | None = None,
                 distro: str = OPERATOR_DISTRO,
                 wsl_home: str = OPERATOR_LINUX_HOME,
                 on_path: dict[str, str] | None = None) -> None:
        super().__init__(env={"USERPROFILE": OPERATOR_HOME,
                              "SystemRoot": r"C:\Windows"})
        self._files = {CX_TOML_PATH: CX_TOML_TEXT,
                       SAY_CMD: "@echo off",
                       CX_SLOT_PATH: "# cx-slot"} if files is None else files
        self._distro = distro
        self._wsl_home = wsl_home
        self._on_path = on_path or {}
        self.calls: list[str] = []

    # every environment read is recorded, so a test can prove a code path
    # never asked WSL anything
    def exists(self, p: Path) -> bool:
        self.calls.append(f"exists:{p}")
        return str(p) in self._files

    def read_text(self, p: Path) -> str:
        self.calls.append(f"read:{p}")
        return self._files.get(str(p), "")

    def which(self, name: str) -> str:
        self.calls.append(f"which:{name}")
        return self._on_path.get(name, "")

    def wsl_distro(self) -> str:
        self.calls.append("wsl_distro")
        return self._distro

    def wsl_home(self) -> str:
        self.calls.append("wsl_home")
        return self._wsl_home


@pytest.fixture
def bare() -> Config:
    """No hub.toml at all — the state a daemon runs in before any install."""
    return Config({}, probe=StubProbe())


@pytest.fixture(autouse=True)
def _no_process_config():
    """Never leak a loaded config between tests, and never read the real one."""
    cfgmod.reset(None)
    yield
    cfgmod.reset(None)


# ── 1. the parity table ──────────────────────────────────────────────────────
# left: what the resolver produces from a bare environment.
# right: the SHAPE the literal had before extraction, rebuilt from the synthetic
#        operator, with the file:line it came from. A red row here means the
#        derivation stopped reproducing what the hardcode used to produce.

def test_parity_hub(bare):
    assert bare.hub.port == 7799                       # daemon.py:35
    assert bare.hub.bind == "0.0.0.0"                  # daemon.py:802
    # the standing title is a PRODUCT name — the hub's universal relay pipe —
    # not an operator's account, so it is a constant here, not a fixture value
    assert bare.hub.standing_title == "chris"          # engine.py:44 HUB_TITLE
    assert bare.hub.standing_session == "hub-chris-standing"   # daemon.py:36
    assert bare.hub.register_standing_title is True    # daemon.py:794


def test_parity_paths(bare):
    p, H = bare.paths, OPERATOR_HOME
    assert p.base_bin == "base"                                  # engine.py:709
    assert p.base_gbl == Path(rf"{H}\.base-gbl")
    assert p.base_store == Path(rf"{H}\.base-gbl\.base")          # engine.py:25
    assert p.claude_home == Path(rf"{H}\.claude")                 # engine.py:28
    assert p.claude_home / "projects" == Path(rf"{H}\.claude\projects")
    assert p.hook_events == Path(rf"{H}\.base\hook-events.jsonl")  # engine.py:415
    assert p.sound_dir == Path(r"C:\Windows\Media")               # daemon.py:459
    # daemon.py:258-259 — both renderings of the same doc, one derived from the
    # other, which is the property that mattered
    assert p.gated_doc == f"{H}/.base-gbl/PROCESS-gated-build.md".replace("\\", "/")
    assert p.gated_doc_wsl == "/mnt/c/Users/operator/.base-gbl/PROCESS-gated-build.md"


def test_parity_wsl(bare):
    w, L, D = bare.wsl, OPERATOR_LINUX_HOME, OPERATOR_DISTRO
    assert w.enabled is True
    assert w.distro == D
    assert w.home_linux == L                                    # daemon.py:88
    assert w.unc_root == rf"\\wsl.localhost\{D}"
    assert w.home_unc == rf"\\wsl.localhost\{D}\home\operator"   # daemon.py:89
    assert w.base_bin == f"{L}/.local/bin/base"                 # wsl-bridge.py:32
    assert w.bridge_port == 7798                                # engine.py:41
    # daemon.py:115 (UNC write) and daemon.py:128 (Linux exec) — one key, two
    # renderings; them drifting apart is a silent broken spawn
    assert w.bridge_deploy_linux == f"{L}/.local/share/hub-bridge"
    assert w.bridge_deploy_unc == (
        rf"\\wsl.localhost\{D}\home\operator\.local\share\hub-bridge")


def test_parity_terminal(bare):
    t = bare.terminal
    assert t.adapter == "wt"                                    # daemon.py:125,147
    assert t.wsl_profile == OPERATOR_WT_GUID                    # daemon.py:67
    assert t.windows_profile == "PowerShell"                    # daemon.py:147
    assert t.restore_focus is True                              # daemon.py:155


def test_parity_spawn(bare):
    s = bare.spawn
    assert s.disallowed_tools == ["AskUserQuestion"]            # daemon.py:636
    assert s.opus_1m is True                                    # daemon.py:640
    assert s.pre_trust is True                                  # daemon.py:644
    assert s.default_model == ""


def test_parity_voice(bare):
    assert bare.stt.enabled is True
    assert bare.stt.url == "http://127.0.0.1:8973/inference"    # daemon.py:508
    assert bare.stt.ffmpeg == "ffmpeg"                          # daemon.py:502
    assert bare.tts.enabled is True
    # daemon.py:450 and daemon.py:530 — the same argv prefix in both places
    assert bare.tts.command == ["cmd", "/c", SAY_CMD]
    assert bare.tts.default_voice == "af_heart"                 # daemon.py:522


def test_parity_cx_ptt(bare):
    c = bare.cx_ptt
    assert c.enabled is True
    assert c.cx_toml == Path(CX_TOML_PATH)                      # engine.py:26
    assert c.cx_slot == Path(CX_SLOT_PATH)                      # engine.py:27
    assert c.python == "python"                                 # engine.py:486


# ── 2. precedence and expansion ──────────────────────────────────────────────
def test_file_value_beats_derivation():
    c = Config({"hub": {"port": 7801},
                "paths": {"gated_doc": "D:/docs/PROCESS.md"}},
               probe=StubProbe())
    assert c.hub.port == 7801
    assert c.paths.gated_doc == "D:/docs/PROCESS.md"
    # a derived sibling still follows the overridden root
    assert c.paths.gated_doc_wsl == "/mnt/d/docs/PROCESS.md"


def test_empty_string_means_derive():
    """The schema ships user-specific keys as "" so the file itself carries no
    operator literal; empty must mean 'derive', not 'use an empty path'."""
    c = Config({"paths": {"gated_doc": ""}, "wsl": {"home_linux": ""}},
               probe=StubProbe())
    assert c.paths.gated_doc.endswith("/.base-gbl/PROCESS-gated-build.md")
    assert c.wsl.home_linux == OPERATOR_LINUX_HOME


def test_tilde_expands_against_the_probe_env():
    c = Config({"paths": {"hub_home": "~/voice"}}, probe=StubProbe())
    assert c.paths.hub_home == Path(rf"{OPERATOR_HOME}\voice")


def test_config_path_resolution_is_env_only(tmp_path, monkeypatch):
    monkeypatch.delenv("BASE_GBL", raising=False)
    monkeypatch.delenv("PING_HUB_CONFIG", raising=False)   # conftest pins it
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    assert cfgmod.config_path() is None            # no file is a valid state
    (tmp_path / ".base-gbl").mkdir()
    (tmp_path / ".base-gbl" / "hub.toml").write_text("[hub]\nport = 1\n")
    assert cfgmod.config_path() == tmp_path / ".base-gbl" / "hub.toml"
    explicit = tmp_path / "other.toml"
    explicit.write_text("[hub]\nport = 2\n")
    monkeypatch.setenv("PING_HUB_CONFIG", str(explicit))
    assert cfgmod.config_path() == explicit
    assert cfgmod.load().hub.port == 2


def test_malformed_config_is_louder_than_a_missing_one(tmp_path):
    bad = tmp_path / "hub.toml"
    bad.write_text("[hub\nport = ")
    with pytest.raises(RuntimeError, match="unreadable"):
        cfgmod.load(bad)


# ── 3. single-side collapse (the Mac posture) ────────────────────────────────
def test_wsl_disabled_collapses_to_one_side():
    c = Config({"wsl": {"enabled": False}}, probe=StubProbe())
    assert c.wsl.enabled is False


def test_unresolvable_distro_disables_the_side_rather_than_guessing():
    """A Mac, or a Windows box with no WSL. Absent is reported as absent — the
    resolver must not fall back to a distro name it invented."""
    probe = StubProbe(distro="", wsl_home="")
    c = Config({}, probe=probe)
    assert c.wsl.enabled is False
    assert c.wsl.distro == ""
    assert c.wsl.unc_root == ""
    assert c.wsl.home_unc == ""          # never a half-built \\wsl.localhost\
    assert c.wsl.bridge_deploy_unc == ""


def test_windows_only_paths_are_never_probed_when_the_side_is_off():
    probe = StubProbe()
    c = Config({"wsl": {"enabled": False}}, probe=probe)
    assert c.hub.port == 7799 and c.paths.base_gbl
    assert not any(x.startswith("wsl_") for x in probe.calls)


# ── 4. adoption vs provisioning (the opinionated-package ruling) ─────────────
def test_bundled_engine_wins_over_an_existing_install():
    """A fresh machine gets the provisioned engine; a machine that already has
    one is adopted. Both are 'voice works out of the box' — the ruling, not a
    flag."""
    bundled = rf"{OPERATOR_HOME}\.ping-hub\tts\say.cmd"
    probe = StubProbe(files={bundled: "@echo off", SAY_CMD: "@echo off"})
    assert Config({}, probe=probe).tts.command == ["cmd", "/c", bundled]


def test_absent_engine_reports_absent_not_a_broken_command():
    """Honesty envelope: absent is not empty-string-that-looks-runnable."""
    probe = StubProbe(files={})
    assert Config({}, probe=probe).tts.command == []


def test_say_on_path_is_adopted_when_nothing_else_exists():
    probe = StubProbe(files={}, on_path={"say": "/usr/local/bin/say"})
    assert Config({}, probe=probe).tts.command == ["/usr/local/bin/say"]


def test_cx_ptt_absent_is_a_capability_answer_not_an_error():
    probe = StubProbe(files={})
    c = Config({}, probe=probe)
    assert c.cx_ptt.enabled is False
    assert c.terminal.wsl_profile == ""      # no cx.toml -> WT picks its default


def test_voice_flags_are_human_overrides_only():
    """Detection never writes `enabled`. Only a human turning voice off does —
    so a missing server must not silently disable the feature."""
    probe = StubProbe(files={})
    assert Config({}, probe=probe).stt.enabled is True
    assert Config({"stt": {"enabled": False}}, probe=probe).stt.enabled is False


# ── 5. shadow-run isolation (G0 amendment 3 — schema half; the live proof is
#      a G2 case) ─────────────────────────────────────────────────────────────
def test_shadow_instance_config_cannot_touch_the_live_registration(tmp_path):
    c = Config({"hub": {"port": 7801, "register_standing_title": False},
                "paths": {"base_gbl": str(tmp_path / "scratch")}},
               probe=StubProbe())
    assert c.hub.register_standing_title is False
    assert c.hub.port != 7799
    scratch = str(tmp_path / "scratch")
    assert str(c.paths.base_gbl) == scratch
    assert str(c.paths.base_store).startswith(scratch)   # journals stay in scratch
    # and specifically NOT the live store the running daemon writes to
    live = Path(OPERATOR_HOME) / ".base-gbl" / ".base"
    assert c.paths.base_store != live
    assert not str(c.paths.base_store).startswith(str(live))


# ── 6. the tripwire ──────────────────────────────────────────────────────────
# SHAPE-based, not identity-based. The first version of this listed one
# person's username and home directory as regexes, which caught his literals
# but published his name into a repo that later went public — the guard was
# itself the leak. Matching the SHAPE of an absolute user path catches anyone's,
# including the next contributor's on their first commit, and names nobody.

_OPERATOR_PATHS = [
    # C:\Users\<someone>  /  C:/Users/<someone>
    (re.compile(r"[A-Za-z]:[\\/]{1,2}Users[\\/]{1,2}(?!<)\w"), "windows home"),
    # /mnt/<drive>/Users/<someone> — the WSL view of the same thing
    (re.compile(r"/mnt/[a-z]/Users/(?!<)\w", re.I), "windows home via WSL"),
    # /home/<someone> and /Users/<someone>, unless it is a placeholder
    (re.compile(r"/home/(?!<|\{|\$)\w"), "linux home"),
    (re.compile(r"(?<!mnt/c)(?<!\w)/Users/(?!<|\{|\$)\w"), "mac home"),
    # a concrete Windows Terminal / COM profile id belongs in hub.toml
    (re.compile(r"\{[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\}",
                re.I), "device/profile GUID"),
]

_SRC = Path(__file__).resolve().parents[1] / "src" / "ping_hub"


def scan_for_operator_paths(text: str) -> list[str]:
    """The rule, as a function, so a test can prove it catches a violation
    instead of only observing that it stays quiet."""
    out = []
    for n, line in enumerate(text.splitlines(), 1):
        for pat, label in _OPERATOR_PATHS:
            if pat.search(line):
                out.append(f"{n}: [{label}] {line.strip()[:90]}")
    return out


def _src_hits() -> list[str]:
    hits = []
    for f in sorted(list(_SRC.rglob("*.py")) + list(_SRC.rglob("*.html"))):
        if "__pycache__" in f.parts:
            continue
        try:
            text = f.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        hits += [f"{f.relative_to(_SRC)}:{h}" for h in scan_for_operator_paths(text)]
    return hits


def test_no_hardcoded_operator_paths_in_src():
    """Nobody's home directory ships in this package — not the author's, and
    not the next contributor's."""
    hits = _src_hits()
    assert not hits, "hardcoded operator paths in src:\n" + "\n".join(hits)


@pytest.mark.parametrize("violation", [
    r'HOME = "C:\\Users\\someone\\.base-gbl"',
    'HOME = "C:/Users/someone/.base-gbl"',
    'WSL_HOME = "/home/someone"',
    'MAC_HOME = "/Users/someone/Library"',
    'DOC = "/mnt/c/Users/someone/notes.md"',
    'PROFILE = "{0a1b2c3d-4e5f-6789-abcd-ef0123456789}"',
])
def test_the_tripwire_actually_catches_a_planted_violation(violation):
    """A guard that has never failed is a guard nobody has tested."""
    assert scan_for_operator_paths(violation), f"tripwire missed: {violation}"


@pytest.mark.parametrize("innocent", [
    'return Path(self.home_linux) / ".local" / "bin"',
    'unc = rf"\\\\wsl.localhost\\{self.distro}"',
    '"""project-dir name for a cwd: C:\\\\code\\\\api -> C--code-api."""',
    'home = "~/.ping-hub"',
    'url = "http://127.0.0.1:8973/inference"',
])
def test_the_tripwire_does_not_fire_on_derived_code(innocent):
    """It has to stay quiet on the derivations that replaced the literals, or
    it would just push people back to hardcoding."""
    assert not scan_for_operator_paths(innocent), f"false positive: {innocent}"


def test_config_module_itself_is_clean():
    """The resolver is the one file that could legitimately have carried the
    defaults. It carries none — that is amendment 1, checked."""
    assert not scan_for_operator_paths(
        (_SRC / "config.py").read_text(encoding="utf-8"))
