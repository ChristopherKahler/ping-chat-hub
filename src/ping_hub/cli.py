"""`ping-hub` — serve, install, doctor, config.

Two halves, deliberately. `install` is scripted and takes `--yes`, because most
machines are ordinary. `doctor` exists because some are not: a fixed installer
dies on the first unexpected machine state, and this package's audience runs
Claude Code, so the escape hatch is a diagnostic an agent can read and act on
rather than a wizard with more branches (Chris's own logged rationale from the
Parakeet installer, 2026-08-15).
"""
from __future__ import annotations

from ping_hub import proc

import argparse
import os
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
    src = args.source or install.installed_source()
    if src:
        sections["update"] = {"source": src}
    else:
        print("\nnote: could not tell what this package was installed from, so "
              "`ping-hub update` will need --source or [update] source in "
              "hub.toml.")
    if args.no_voice:
        print("\n--no-voice: skipping engine provisioning")
    else:
        stt = install.provision_stt(home)
        sections["stt"] = {"launcher": stt["launcher"],
                           "model_dir": stt["model_dir"]}
        tts = install.provision_tts(home)
        sections["tts"] = {"command": tts["command"]}
        # cx-ptt is Windows-only (it binds global hotkeys and winsound). On a
        # Mac that is a machine without the feature, not a failed install.
        if os.name == "nt":
            cx = install.provision_cxptt(home, cfg, stt)
            sections["cx_ptt"] = {"launcher": cx["launcher"], "autostart": True}
        else:
            print("\ncx-ptt: Windows only — skipping on this machine")

    want_bridge, why = install.bridge_decision(
        cfg, force=args.deploy_bridge, skip=args.no_deploy_bridge)
    print(f"\nWSL bridge: {'deploying' if want_bridge else 'skipping'} — {why}")
    if want_bridge:
        b = install.deploy_bridge(cfg, python=autostart.bridge_python(cfg))
        print(f"bridge deployed to {b['linux_path']}")
        print("bridge autostart:")
        for line in autostart.register_bridge(cfg):
            print(f"  {line}")

    target = Path(args.config) if args.config else cfg.paths.base_gbl / "hub.toml"
    install.write_hub_toml(target, sections)
    # reload FIRST: the login tasks point at what provisioning just wrote, so
    # they have to be planned from the new config, not the pre-install one
    config.reset(config.load(target))
    print("\nlogin registration:")
    for line in register_autostart(config.get(), skip=args.no_autostart):
        print(f"  {line}")
    if not args.no_autostart:
        # verified, THEN removed — never the other way round
        for line in autostart.supersede_interim_run_key(config.get()):
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


def daemon_is_running(cfg, connect=None) -> bool:
    """Something is already serving the hub port. Updating under a live daemon
    swaps files beneath a running process; the honest move is to refuse."""
    import socket
    connect = connect or (lambda h, p: socket.create_connection((h, p), 1.0))
    host = "127.0.0.1" if cfg.hub.bind in ("0.0.0.0", "") else cfg.hub.bind
    try:
        connect(host, cfg.hub.port).close()
        return True
    except (OSError, AttributeError):
        return False


def update_command(cfg, source: str = "") -> list[str]:
    """The argv `update` would run. Split out so it can be asserted without
    installing anything."""
    src = source or cfg.update.source
    if not src:
        raise install.InstallError(
            "no update source recorded. Re-run `ping-hub install` from the "
            "package you want to track, or set [update] source in hub.toml.")
    return [sys.executable, "-m", "pip", "install", "--upgrade", src]


def cmd_update(args) -> int:
    cfg = config.get()
    if daemon_is_running(cfg) and not args.force:
        print(f"the hub is serving on port {cfg.hub.port}. Stop it first, or "
              f"pass --force to update underneath it.")
        return 1
    cmd = update_command(cfg, args.source or "")
    print("  " + " ".join(cmd))
    if args.dry_run:
        return 0
    import subprocess
    r = proc.run(cmd, capture_output=True, text=True)
    print((r.stdout or "").strip()[-1500:] or "(no output)")
    if r.returncode != 0:
        print((r.stderr or "").strip()[-800:], file=sys.stderr)
        return r.returncode
    print("\nre-checking:")
    return cmd_doctor(args)


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
    i.add_argument("--source", help="record what `ping-hub update` reinstalls "
                                    "from (default: detected from pip)")
    i.add_argument("--no-autostart", action="store_true",
                   help="do not register the hub to start at login")
    i.add_argument("--deploy-bridge", action="store_true",
                   help="deploy the WSL bridge even when one is already there "
                        "(it may be running right now)")
    i.add_argument("--no-deploy-bridge", action="store_true",
                   help="skip the WSL bridge entirely")
    i.set_defaults(fn=cmd_install)

    d = sub.add_parser("doctor", help="what works on this machine, and why not")
    d.set_defaults(fn=cmd_doctor)

    c = sub.add_parser("config", help="print every resolved value")
    c.set_defaults(fn=cmd_config)

    u = sub.add_parser("update", help="reinstall this package from its source")
    u.add_argument("--source", help="override the recorded source")
    u.add_argument("--dry-run", action="store_true", help="print the command only")
    u.add_argument("--force", action="store_true",
                   help="update even while the hub is serving")
    u.set_defaults(fn=cmd_update)

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
