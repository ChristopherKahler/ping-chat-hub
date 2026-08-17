"""`ping-hub` — serve, install, doctor, config.

Two halves, deliberately. `install` is scripted and takes `--yes`, because most
machines are ordinary. `doctor` exists because some are not: a fixed installer
dies on the first unexpected machine state, and this package's audience runs
Claude Code, so the escape hatch is a diagnostic an agent can read and act on
rather than a wizard with more branches (Chris's own logged rationale from the
Parakeet installer, 2026-08-15).
"""
from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

from ping_hub import autostart, capabilities, config, install


def _preflight(cfg) -> list[tuple[str, bool, str]]:
    """Everything that has to be true before provisioning starts, checked all
    at once so the operator sees the whole list, not the first failure."""
    out = []
    v = sys.version_info
    out.append(("python >= 3.11", v >= (3, 11), f"{v.major}.{v.minor}.{v.micro}"))
    b = shutil.which(cfg.paths.base_bin)
    out.append((f"`{cfg.paths.base_bin}` on PATH", bool(b), b or "not found"))
    c = shutil.which("claude")
    out.append(("`claude` on PATH", bool(c), c or "not found"))
    f = shutil.which(cfg.stt.ffmpeg)
    out.append((f"`{cfg.stt.ffmpeg}` on PATH", bool(f),
                f or "not found (needed to convert phone audio)"))
    try:
        free = shutil.disk_usage(cfg.paths.hub_home.parent).free
    except OSError:
        free = 0
    out.append(("4 GB free", free > 4 * 1024 ** 3, f"{free / 1024 ** 3:.1f} GB"))
    return out


def _print_checks(checks) -> bool:
    for name, ok, detail in checks:
        print(f"  {'OK  ' if ok else 'FAIL'}  {name:28} {detail}")
    return all(ok for _, ok, _ in checks)


def cmd_install(args) -> int:
    cfg = config.get()
    home = Path(args.home) if args.home else cfg.paths.hub_home
    print(f"ping-hub install -> {home}")
    print("\npreflight:")
    ok = _print_checks(_preflight(cfg))
    if not ok and not args.force:
        print("\npreflight failed. Fix the above, or re-run with --force to "
              "install anyway (voice will be incomplete).")
        return 1
    if not args.yes:
        print(f"\nThis downloads ~800 MB of speech models into {home} and "
              f"builds two isolated venvs.\nNothing is installed into "
              f"{sys.executable}.")
        if input("proceed? [y/N] ").strip().lower() not in ("y", "yes"):
            return 1

    sections: dict[str, dict] = {"paths": {"hub_home": str(home)}}
    if args.no_voice:
        print("\n--no-voice: skipping engine provisioning")
    else:
        stt = install.provision_stt(home)
        sections["stt"] = {"launcher": stt["launcher"],
                           "model_dir": stt["model_dir"]}
        tts = install.provision_tts(home)
        sections["tts"] = {"command": tts["command"]}

    if args.deploy_bridge:
        # off by default: this writes into a live WSL home, and on a two-sided
        # machine the bridge it replaces may be running right now
        b = install.deploy_bridge(cfg)
        print(f"\nbridge deployed. Start it in WSL with:\n  python3 {b['linux_path']}")

    target = Path(args.config) if args.config else cfg.paths.base_gbl / "hub.toml"
    install.write_hub_toml(target, sections)
    # reload FIRST: the login tasks point at what provisioning just wrote, so
    # they have to be planned from the new config, not the pre-install one
    config.reset(config.load(target))
    print("\nlogin registration:")
    for line in register_autostart(config.get(), skip=args.no_autostart):
        print(f"  {line}")

    print("\nre-checking with the new config:")
    cmd_doctor(args)
    print(f"\ndone. `ping-hub serve` starts the hub on "
          f"{config.get().hub.bind}:{config.get().hub.port}.")
    return 0


def register_autostart(cfg, skip: bool = False, **kw) -> list[str]:
    """Register the hub (and the speech server, if one was provisioned) to
    start at login. On by default: "works out of the box" includes surviving a
    reboot, and a hub that has to be started by hand is a hub that is down when
    you reach for your phone."""
    if skip:
        return ["skipped (--no-autostart)"]
    try:
        return autostart.register(cfg, **kw)
    except OSError as e:
        # a failed task registration must not lose a good install
        return [f"could not register login tasks: {e}",
                "the hub is installed; start it with `ping-hub serve`"]


def cmd_doctor(args) -> int:
    cfg = config.get()
    print(f"config: {cfg.source or '(none — everything derived)'}")
    caps = capabilities.probe_all(cfg)
    for name, r in caps.items():
        print(f"  {r['state']:7} {name:8} {r['detail']}")
    bad = [n for n, r in caps.items() if r["state"] == capabilities.ERROR]
    if bad:
        print(f"\nnot responding: {', '.join(bad)}")
        return 1
    return 0


def cmd_config(args) -> int:
    cfg = config.get()
    print(f"source: {cfg.source or '(none — everything derived)'}")
    for sec in ("hub", "paths", "wsl", "terminal", "spawn", "stt", "tts", "cx_ptt"):
        print(f"\n[{sec}]")
        obj = getattr(cfg, sec)
        for key in sorted(k for k in dir(type(obj)) if not k.startswith("_")):
            try:
                print(f"  {key:22} {getattr(obj, key)}")
            except Exception as e:            # a probe that cannot answer
                print(f"  {key:22} <unresolved: {e}>")
    return 0


def cmd_serve(args) -> int:
    from ping_hub import daemon
    daemon.main()
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="ping-hub")
    sub = p.add_subparsers(dest="cmd")

    s = sub.add_parser("serve", help="run the hub daemon")
    s.set_defaults(fn=cmd_serve)

    i = sub.add_parser("install", help="provision the bundled speech engines")
    i.add_argument("--yes", "-y", action="store_true", help="no prompts")
    i.add_argument("--home", help="install dir (default: ~/.ping-hub)")
    i.add_argument("--config", help="hub.toml to write")
    i.add_argument("--no-voice", action="store_true", help="config only")
    i.add_argument("--force", action="store_true", help="ignore preflight failures")
    i.add_argument("--no-autostart", action="store_true",
                   help="do not register the hub to start at login")
    i.add_argument("--deploy-bridge", action="store_true",
                   help="copy the WSL bridge into WSL (overwrites a deployed "
                        "one, which may be running)")
    i.set_defaults(fn=cmd_install)

    d = sub.add_parser("doctor", help="what works on this machine, and why not")
    d.set_defaults(fn=cmd_doctor)

    c = sub.add_parser("config", help="print every resolved value")
    c.set_defaults(fn=cmd_config)

    a = p.parse_args(argv)
    if not getattr(a, "fn", None):
        p.print_help()
        return 1
    try:
        return a.fn(a)
    except install.InstallError as e:
        print(f"\ninstall failed: {e}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
