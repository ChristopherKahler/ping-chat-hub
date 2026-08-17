"""Terminal adapters — how a new session gets a real terminal.

"New chat" opens an actual terminal tab, not a pty the hub owns (Chris
directive). Which terminal is the one genuinely platform-shaped thing in the
hub, so it is the one thing behind a seam:

    wt            Windows Terminal — built, and what this machine runs
    tmux          Mac/Linux — declared, NOT built this pass
    iterm2        Mac — declared, NOT built this pass

An unbuilt adapter raises `AdapterNotBuilt` by name. It does not silently fall
back to `wt`, which on a Mac would be a spawn that fails with a confusing
error from a program that is not installed.
"""
from __future__ import annotations


class AdapterNotBuilt(NotImplementedError):
    """Named in the schema, not implemented yet. Says which, and what to do."""


def get(name: str):
    """Resolve an adapter module by its `[terminal].adapter` name."""
    if name == "wt":
        from ping_hub.spawn import wt
        return wt
    if name in ("tmux", "iterm2", "terminal_app"):
        raise AdapterNotBuilt(
            f"terminal adapter '{name}' is designed but not built yet "
            f"(Mac support is design-only this pass). Set [terminal].adapter "
            f"= \"wt\" on Windows, or build the adapter.")
    raise AdapterNotBuilt(f"unknown terminal adapter '{name}'; known: "
                          f"wt, tmux, iterm2, terminal_app")


def spawn(cfg, side: str, claude_args: list[str], cwd: str | None = None,
          title: str | None = None, prompt: str | None = None) -> None:
    get(cfg.terminal.adapter).spawn(cfg, side, claude_args, cwd, title, prompt)
