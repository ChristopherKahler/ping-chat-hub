"""``claude-chat`` — the CLI entry point.

    setup · serve · open · enable · disable · status · test · say
    mode · model · updates · host · workspace · hostname
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path


def _build_parser() -> argparse.ArgumentParser:
    from claude_chat import __version__

    parser = argparse.ArgumentParser(
        prog="claude-chat",
        description="Claude Code sessions in a browser — any directory, saved "
                    "workspaces, live telemetry, Allow/Deny approvals.")
    parser.add_argument("--version", action="version",
                        version=f"claude-chat {__version__}")
    sub = parser.add_subparsers(dest="command", metavar="<command>")

    sub.add_parser("setup", help="Wizard: port + permission mode. No external app.")
    sub.add_parser("serve", help="Run the daemon in the foreground (UI + API).")
    sub.add_parser("open", help="Print the chat UI url and try to open a browser.")
    sub.add_parser("enable", help="Install and start the daemon as a background service.")
    sub.add_parser("disable", help="Stop and remove the service.")
    sub.add_parser("status", help="Service state, config summary, counts.")
    sub.add_parser("test", help="Round-trip the daemon's state endpoint — wiring check.")

    mode = sub.add_parser(
        "mode", help="Show or set the permission posture (approve|skip).")
    mode.add_argument("value", nargs="?", choices=["approve", "skip"], default=None)
    model = sub.add_parser(
        "model", help="Show or set the turn model ('default' clears the override).")
    model.add_argument("value", nargs="?", default=None)
    updates = sub.add_parser(
        "updates", help="Show or toggle in-turn proactive narration (on|off).")
    updates.add_argument("value", nargs="?", choices=["on", "off"], default=None)
    host = sub.add_parser(
        "host", help="Show or set the bind: local, tailscale, all, or an IPv4.")
    host.add_argument("value", nargs="?", default=None)

    say = sub.add_parser(
        "say", help="Post into a conversation — a session's mid-turn voice.")
    say.add_argument("text", help="Message text to post.")
    say.add_argument("--conversation", default=None,
                     help="Conversation id (defaults to $CLAUDE_CHAT_CONVERSATION).")

    # -- workspace ----------------------------------------------------------
    workspace = sub.add_parser(
        "workspace", help="The saved directories the UI quick-selects from.")
    wsub = workspace.add_subparsers(dest="workspace_command", metavar="<command>")
    wsub.add_parser("list", help="Every saved workspace.")
    add = wsub.add_parser("add", help="Register a directory (re-adding an id updates it).")
    add.add_argument("path", type=Path)
    add.add_argument("--id", dest="workspace_id", default="",
                     help="Short id for @shorthand (default: the folder name).")
    add.add_argument("--name", default="", help="Display name (default: the folder name).")
    add.add_argument("--model", default="",
                     help="Model for sessions here (default: the daemon's).")
    add.add_argument("--mode", default="", choices=["", "approve", "skip"],
                     help="Permission mode here (default: the daemon's).")
    add.add_argument("--boot", default="",
                     help="Prefix for a fresh session's first turn, e.g. a slash command.")
    add.add_argument("--dir", dest="dirs", action="append", default=[],
                     help="Extra readable root (--add-dir). Repeatable.")
    add.add_argument("--default", action="store_true",
                     help="Make this the workspace new conversations start in.")
    remove = wsub.add_parser("remove", help="Forget a workspace.")
    remove.add_argument("workspace_id")
    default = wsub.add_parser("default", help="Show or set the default workspace.")
    default.add_argument("workspace_id", nargs="?", default=None)
    imp = wsub.add_parser(
        "import", help="Seed from a TOML file of [[workspace]] path entries.")
    imp.add_argument("--from", dest="source", type=Path,
                     default=Path.home() / ".base-gbl" / "base.toml")

    # -- hostname -----------------------------------------------------------
    hostname = sub.add_parser(
        "hostname", help="The friendly url (e.g. chat.go) — hosts entry + :80 vhost.")
    hsub = hostname.add_subparsers(dest="hostname_command", metavar="<command>")
    hset = hsub.add_parser("set", help="Name it (e.g. chat.go) and point links at it.")
    hset.add_argument("name")
    hsub.add_parser("sync", help="(WSL) point the Windows hosts file at this VM's "
                                 "current IP. Idempotent — safe from your shell rc.")
    install = hsub.add_parser(
        "install", help="Root: write the hosts entry and the reverse-proxy vhost.")
    install.add_argument("name", nargs="?", default=None)
    install.add_argument("--proxy", default="",
                         choices=["", "apache2", "nginx", "caddy", "none"],
                         help="Override proxy detection.")
    uninstall = hsub.add_parser("uninstall", help="Root: remove the vhost and hosts entry.")
    uninstall.add_argument("--proxy", default="",
                           choices=["", "apache2", "nginx", "caddy", "none"])
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    from claude_chat import cli

    if args.command == "setup":
        return cli.run_setup()
    if args.command == "serve":
        return cli.run_serve()
    if args.command == "open":
        return cli.run_open()
    if args.command == "enable":
        return cli.run_enable()
    if args.command == "disable":
        return cli.run_disable()
    if args.command == "status":
        return cli.run_status()
    if args.command == "test":
        return cli.run_test()
    if args.command == "mode":
        return cli.run_mode(args.value)
    if args.command == "model":
        return cli.run_model(args.value)
    if args.command == "updates":
        return cli.run_updates(args.value)
    if args.command == "host":
        return cli.run_host(args.value)
    if args.command == "say":
        return cli.run_say(args.text, args.conversation)

    if args.command == "workspace":
        if args.workspace_command == "list":
            return cli.run_workspace_list()
        if args.workspace_command == "add":
            return cli.run_workspace_add(
                args.path, workspace_id=args.workspace_id, name=args.name,
                model=args.model, mode=args.mode, boot=args.boot,
                dirs=args.dirs, make_default=args.default)
        if args.workspace_command == "remove":
            return cli.run_workspace_remove(args.workspace_id)
        if args.workspace_command == "default":
            return cli.run_workspace_default(args.workspace_id)
        if args.workspace_command == "import":
            return cli.run_workspace_import(args.source)
        return cli.run_workspace_list()

    if args.command == "hostname":
        if args.hostname_command == "set":
            return cli.run_hostname_set(args.name)
        if args.hostname_command == "sync":
            return cli.run_hostname_sync()
        if args.hostname_command == "install":
            return cli.run_hostname_install(args.name, args.proxy)
        if args.hostname_command == "uninstall":
            return cli.run_hostname_uninstall(args.proxy)
        return cli.run_hostname_show()

    parser.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
