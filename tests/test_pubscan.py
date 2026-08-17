"""The publication scanner.

Two jobs, and the second is the one that matters. It has to catch a planted
violation of every shape it claims to cover, and it has to stay quiet on the
placeholder-and-derivation style that replaced those literals — a scanner that
fires on `Path.home()` would just teach people to disable it.

The last test runs it against this repo for real. That assertion is the guard
itself: if anyone commits their own home directory, an email, or a key, this
goes red before the push rather than after.

pubscan: allow-file — this file is made of the shapes it hunts for, so it is
exempt from its own scan. It is the only WHOLE-FILE exemption in the tree; a
handful of lines elsewhere carry `# pubscan: allow`, each on a fabricated
fixture value. Both markers are meant to be rare and visible in review, which
is the point of putting them in the source rather than in a config file.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location(
    "pubscan", REPO / "tools" / "pubscan.py")
pubscan = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(pubscan)


# Violations are COMPOSED at runtime, never written as literals: this file is
# itself scanned by test_this_repo_is_publishable, and a test fixture that
# tripped the guard it is testing would be a guard nobody could keep green.
WHO = "jsm" + "ith"          # a name no placeholder vocabulary would contain


@pytest.mark.parametrize("violation, expect", [
    (r'HOME = "C:\\Users\\%s\\.base-gbl"', "windows home"),
    ('HOME = "C:/Users/%s/.base-gbl"', "windows home"),
    ('DOC = "/mnt/c/Users/%s/notes.md"', "windows home via WSL"),
    ('WSL_HOME = "/home/%s"', "linux home"),
    ('MAC = "/Users/%s/Library/Application Support"', "mac home"),
    ('CONTACT = "%s@' + "realdomain" + '.io"', "email address"),
])
def test_catches_a_real_name_in_every_home_shape(violation, expect):
    line = violation % WHO
    found = pubscan.scan_text(line)
    assert found, f"missed entirely: {line}"
    assert expect in {label for _, label, _ in found}, (
        f"caught {line!r} but labelled it {[l for _, l, _ in found]}")


@pytest.mark.parametrize("violation, expect", [
    ('PROFILE = "{%s-4e5f-6789-abcd-ef0123456789}"' % "0a1b2c3d",
     "device or profile GUID"),
    ('URL = "https://my-desktop.%s.ts.net:8443"' % "tail1a2b3c", "tailnet name"),
    ('PEER = "100.%s.178.121"' % "89", "tailnet address"),
    ('api_key = "sk-%s"' % ("abcdefghijklmnop"), "credential assignment"),
    ('PASSWORD: "%s"' % "hunter2hunter2", "credential assignment"),
    ("-----BEGIN OPENSSH %s KEY-----" % "PRIVATE", "private key"),
])
def test_catches_every_machine_and_secret_shape(violation, expect):
    found = pubscan.scan_text(violation)
    assert found, f"missed entirely: {violation}"
    assert expect in {label for _, label, _ in found}


@pytest.mark.parametrize("innocent", [
    'return Path.home() / ".ping-hub"',
    'home = "~/.local/share/hub-bridge"',
    '"""C:/Users/<user>/f.md -> /mnt/c/Users/<user>/f.md."""',
    'path = f"{home}/.claude/projects"',
    'unc = rf"\\\\wsl.localhost\\{distro}"',
    'url = "http://127.0.0.1:8973/inference"',
    'bind = "0.0.0.0"',
    'LOCAL = "/home/${USER}/bin"',
    'token = payload.get("token")',
])
def test_stays_quiet_on_placeholders_and_derivations(innocent):
    assert not pubscan.scan_text(innocent), f"false positive: {innocent}"


@pytest.mark.parametrize("example", [
    r'ready   tts      C:\Users\you\.ping-hub\tts\say.cmd',
    "ready   wsl      Ubuntu at /home/you",
    'home_linux = "/home/operator"',
    'placeholder="/home/you/project"',
    'CONTACT = "someone@example.com"',
])
def test_documentation_examples_are_allowed(example):
    """Docs and tests are SUPPOSED to show example paths. A guard that forbids
    them gets switched off, which is worse than no guard."""
    assert not pubscan.scan_text(example), f"blocked an example: {example}"


def test_skips_binaries_and_build_output():
    root = REPO
    assert not pubscan.should_scan(root / "src" / "ping_hub" / "assets" / "icon-512.png", root)
    assert not pubscan.should_scan(root / "build" / "lib" / "x.py", root)
    assert not pubscan.should_scan(root / ".git" / "config", root)
    assert pubscan.should_scan(root / "README.md", root)


def test_does_not_flag_itself():
    """It contains every shape it hunts for, as regex source."""
    root = REPO
    assert not pubscan.should_scan(root / "tools" / "pubscan.py", root)


def test_this_repo_is_publishable():
    """The guard. Red here means something identifying is about to be pushed."""
    hits, scanned = pubscan.scan_tree(REPO)
    assert scanned > 10, f"scanner found almost nothing to read ({scanned} files)"
    assert not hits, "content that should not be published:\n" + "\n".join(
        f"  {p}: {f}" for p, f in hits.items())


def test_exit_code_is_usable_in_a_hook(tmp_path, capsys):
    clean = tmp_path / "clean"
    clean.mkdir()
    (clean / "a.py").write_text('x = Path.home()\n', encoding="utf-8")
    assert pubscan.main([str(clean)]) == 0
    (clean / "b.py").write_text(f'H = "/home/{WHO}"\n', encoding="utf-8")
    assert pubscan.main([str(clean)]) == 1
    assert "linux home" in capsys.readouterr().out
