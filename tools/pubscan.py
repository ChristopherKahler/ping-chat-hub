#!/usr/bin/env python3
"""Pre-publication scan — what in this tree identifies its author, or leaks.

This repo is public. That makes a class of content a defect which was harmless
while it was private: a home directory with a real username in it, a device id,
a tailnet hostname, an API key someone pasted into a comment.

    python tools/pubscan.py .          # exit 1 if anything is flagged

The in-suite tripwire in `tests/test_ping_hub_config.py` checks `src/` only, and
only for hardcoded paths, because its job is keeping the package portable. This
one covers the WHOLE tree — docs, README, tests, config — and looks for secrets
too. Written after the repo went public with two test files carrying the
author's usernames; the fix was cheap because nobody had the link yet, and this
exists so the next one is caught before the push rather than after it.

Patterns are SHAPE-based on purpose. An identity-based rule has to spell out the
name it is protecting, which puts the name in the repo — the guard becomes the
leak. Shape rules also protect the next contributor, who is not the author.

**What this is and is not.** It catches the ACCIDENT: your own home directory,
your tailnet host, a key you pasted while debugging. It does not catch a
determined leak, because it cannot tell a real account name from an invented
one — it decides by vocabulary. `/home/you` and `C:\\Users\\operator` pass as
example paths; `/home/jsmith` does not. Extend PLACEHOLDERS when you need a new
stand-in, and prefer `<user>` or `${HOME}`, which never trip it at all.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

# Home-directory patterns capture the account name so it can be checked against
# the placeholder vocabulary below. The rule is "no REAL home directory" — docs
# and tests are supposed to show example paths, and a guard that forbids them
# just gets switched off.
HOME_PATTERNS: list[tuple[str, re.Pattern]] = [
    ("windows home", re.compile(r"[A-Za-z]:[\\/]{1,2}Users[\\/]{1,2}([^\\/\s\"'<>{$]+)")),
    ("windows home via WSL", re.compile(r"/mnt/[a-z]/Users/([^/\s\"'<>{$]+)", re.I)),
    ("linux home", re.compile(r"/home/([^/\s\"'<>{$]+)")),
    ("mac home", re.compile(r"(?<![A-Za-z]:)(?<!mnt/c)(?<!\w)/Users/([^/\s\"'<>{$]+)")),
]

# Conventional stand-ins. Anything here is an example, not a person.
PLACEHOLDERS = {
    "user", "users", "username", "you", "your", "youruser", "yourname",
    "someone", "somebody", "operator", "example", "name", "me", "test",
    "testuser", "foo", "bar", "alice", "bob", "chris_ai", "dev",
}
PLACEHOLDER_DOMAINS = {"example.com", "example.org", "example.net", "localhost"}

# Characters that mean "this is not a literal account name": format specifiers,
# shell/template interpolation, regex metacharacters, ellipses.
NOT_A_NAME = set("%{}$<>()[]|?*+\\!^")

# An explicit escape hatch, the way every secret scanner has one. A file that
# exists to TEST this scanner will always contain scanner-shaped strings.
ALLOW_LINE = "pubscan: allow"
ALLOW_FILE = "pubscan: allow-file"


def is_placeholder(name: str) -> bool:
    """Is this an example rather than somebody's actual account?"""
    bare = name.strip(".").strip()
    if not bare:                       # "/home/..." in prose
        return True
    if NOT_A_NAME & set(bare):         # "%s", "${USER}", "(?!<"
        return True
    return bare.lower() in PLACEHOLDERS

PATTERNS: list[tuple[str, re.Pattern]] = [
    ("device or profile GUID",
     re.compile(r"\{[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\}",
                re.I)),
    ("tailnet name", re.compile(r"[\w-]+\.ts\.net|tail[0-9a-f]{6}")),
    ("tailnet address",
     re.compile(r"\b100\.(6[4-9]|[7-9]\d|1[01]\d|12[0-7])\.\d+\.\d+\b")),
    ("credential assignment",
     re.compile(r"(api[_-]?key|auth[_-]?token|bearer|secret|passwd|password)"
                r"\s*[=:]\s*['\"][^'\"]{8,}", re.I)),
    ("private key", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
]

EMAIL = re.compile(r"[\w.+-]+@([\w-]+\.[a-z]{2,})", re.I)

SKIP_DIRS = {".git", "__pycache__", ".pytest_cache", ".venv", "venv",
             "build", "dist", "node_modules", ".mypy_cache", ".ruff_cache"}
SKIP_SUFFIXES = {".png", ".jpg", ".jpeg", ".gif", ".ico", ".pyc", ".pyo",
                 ".woff", ".woff2", ".ttf", ".zip", ".onnx", ".bin", ".wav"}

# This file necessarily contains the shapes it hunts for.
SELF = Path(__file__).name


def scan_text(text: str) -> list[tuple[int, str, str]]:
    """(line number, label, the line) for everything flagged."""
    out = []
    if ALLOW_FILE in text[:2000]:
        return out
    for n, line in enumerate(text.splitlines(), 1):
        if ALLOW_LINE in line:
            continue
        for label, pat in HOME_PATTERNS:
            for name in pat.findall(line):
                if not is_placeholder(name):
                    out.append((n, label, line.strip()[:100]))
                    break
        for domain in EMAIL.findall(line):
            if domain.lower() not in PLACEHOLDER_DOMAINS:
                out.append((n, "email address", line.strip()[:100]))
                break
        for label, pat in PATTERNS:
            if pat.search(line):
                out.append((n, label, line.strip()[:100]))
    return out


def should_scan(path: Path, root: Path) -> bool:
    rel = path.relative_to(root)
    if any(p in SKIP_DIRS or p.endswith(".egg-info") for p in rel.parts):
        return False
    if path.suffix.lower() in SKIP_SUFFIXES:
        return False
    return path.name != SELF


def scan_tree(root: Path) -> tuple[dict[str, list[tuple[int, str, str]]], int]:
    """{relative path: findings}, and how many files were actually read."""
    hits: dict[str, list[tuple[int, str, str]]] = {}
    scanned = 0
    for f in sorted(root.rglob("*")):
        if not f.is_file() or not should_scan(f, root):
            continue
        try:
            text = f.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        scanned += 1
        found = scan_text(text)
        if found:
            hits[str(f.relative_to(root))] = found
    return hits, scanned


def main(argv: list[str] | None = None) -> int:
    argv = argv if argv is not None else sys.argv[1:]
    root = Path(argv[0] if argv else ".").resolve()
    hits, scanned = scan_tree(root)
    for path, findings in hits.items():
        print(path)
        for n, label, line in findings:
            print(f"  {n}: [{label}] {line}")
    print(f"\nscanned {scanned} files, flagged {len(hits)}")
    if hits:
        print("\nThis tree is published. Replace real values with placeholders "
              "(<user>, ${HOME}) or move them into config that is not committed.")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
