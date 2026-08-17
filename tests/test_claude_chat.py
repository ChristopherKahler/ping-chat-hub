"""The claude-chat suite — store, turn core, rail, hostname wiring.

Every test runs against a throwaway ``CLAUDE_CHAT_HOME``; no test touches the
operator's real state, spawns ``claude``, or writes to /etc.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from claude_chat import hosts, store, turns
from claude_chat.daemon import ChatRail
from claude_chat.turns import TurnResult


_RAILS: list[ChatRail] = []

# Captured BEFORE the autouse fixture stubs the module attribute — the direct
# relay_register test drives the real function.
_REAL_RELAY_REGISTER = turns.relay_register


@pytest.fixture(autouse=True)
def chat_home(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_CHAT_HOME", str(tmp_path / "home"))
    # Session announces must never bind titles into the REAL global relay
    # registry from a test run — the dedicated tests re-patch to capture.
    monkeypatch.setattr(turns, "relay_register", lambda session_id, title: False)
    yield tmp_path
    # A rail's turn pool must not outlive its test — a straggler thread would
    # write into the NEXT test's throwaway home.
    while _RAILS:
        _RAILS.pop().close()


@pytest.fixture
def workdir(tmp_path):
    path = tmp_path / "project"
    path.mkdir()
    return path


def wait_for(predicate, timeout: float = 5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return False


# ---------------------------------------------------------------------------
# store
# ---------------------------------------------------------------------------

def test_config_defaults_then_persist():
    config = store.load_config()
    assert config["mode"] == "approve"
    assert config["port"] == store.DEFAULT_PORT
    assert not store.configured()

    config["port"] = 9999
    store.save_config(config)
    assert store.configured()
    assert store.load_config()["port"] == 9999
    # 0600: the conversation store is the operator's private history
    assert (store.home() / "config.json").stat().st_mode & 0o777 == 0o600


def test_workspace_add_is_idempotent_by_id(workdir):
    first = store.add_workspace(workdir, name="Project")
    again = store.add_workspace(workdir, name="Renamed")
    assert first["id"] == again["id"] == "project"
    assert len(store.load_workspaces()) == 1
    assert store.find_workspace("project")["name"] == "Renamed"


def test_workspace_add_rejects_a_missing_directory(tmp_path):
    with pytest.raises(ValueError):
        store.add_workspace(tmp_path / "nope")


def test_workspace_remove_clears_the_default(workdir):
    entry = store.add_workspace(workdir)
    config = store.load_config()
    config["default_workspace"] = entry["id"]
    store.save_config(config)

    assert store.remove_workspace(entry["id"]) is True
    assert store.load_config()["default_workspace"] == ""
    assert store.remove_workspace(entry["id"]) is False


def test_resolve_cwd_precedence(workdir, tmp_path):
    other = tmp_path / "other"
    other.mkdir()
    entry = store.add_workspace(workdir)

    # explicit path beats everything
    assert store.resolve_cwd(entry, str(other))[0] == str(other)
    # then the named workspace
    assert store.resolve_cwd(entry)[0] == str(workdir)
    # then the default workspace
    config = store.load_config()
    config["default_workspace"] = entry["id"]
    store.save_config(config)
    assert store.resolve_cwd(None)[0] == str(workdir)
    # then default_cwd
    config["default_workspace"] = ""
    config["default_cwd"] = str(other)
    store.save_config(config)
    assert store.resolve_cwd(None)[0] == str(other)
    # then home
    config["default_cwd"] = ""
    store.save_config(config)
    assert store.resolve_cwd(None)[0] == str(Path.home())


def test_prune_threads_drops_stale_entries():
    now = time.time()
    threads = {
        "fresh": {"session_id": "a", "last_turn": now},
        "stale": {"session_id": "b", "last_turn": now - 40 * 86400},
    }
    pruned = store.prune_threads(threads, now=now)
    assert set(pruned) == {"fresh"}


def test_conversation_id_cannot_walk_out_of_the_store():
    assert store.load_conversation("../../etc/passwd") is None
    assert store.load_conversation("c1/../c2") is None


# ---------------------------------------------------------------------------
# turn core
# ---------------------------------------------------------------------------

def test_parse_scope():
    assert turns.parse_scope("@toolbox fix the tests") == ("toolbox", "fix the tests")
    assert turns.parse_scope("no scope here") == ("", "no scope here")
    assert turns.parse_scope("@lonely") == ("", "@lonely")


def test_compose_prompt_fresh_carries_boot_and_protocol():
    prompt = turns.compose_prompt("ship it", resumed=False, boot="/resume-session",
                                  surface="claude-chat", say_cmd="claude-chat say")
    assert prompt.startswith("/resume-session\n\nship it")
    assert "rail protocol" in prompt
    assert "claude-chat say" in prompt


def test_compose_prompt_resumed_is_verbatim():
    prompt = turns.compose_prompt("and now the tests", resumed=True, boot="/boot",
                                  surface="claude-chat", say_cmd="say")
    assert prompt == "and now the tests"


def test_compose_prompt_without_updates_has_no_protocol():
    prompt = turns.compose_prompt("quiet please", resumed=False, updates=False,
                                  surface="claude-chat", say_cmd="say")
    assert prompt == "quiet please"


def test_build_cmd_approve_mode_gates_with_the_permission_tool():
    cmd = turns.build_cmd("/bin/claude", mode="approve", prompt="hi",
                          mcp_config="/tmp/x.json", model="opus",
                          add_dirs=["/extra"], resume="sess-1")
    assert "--permission-prompt-tool" in cmd
    assert cmd[cmd.index("--permission-prompt-tool") + 1] == turns.APPROVE_TOOL
    assert "--dangerously-skip-permissions" not in cmd
    assert cmd[cmd.index("--add-dir") + 1] == "/extra"
    assert cmd[cmd.index("--resume") + 1] == "sess-1"
    assert cmd[cmd.index("--model") + 1] == "opus"
    assert cmd[-2:] == ["-p", "hi"]


def test_build_cmd_skip_mode_drops_the_gate():
    cmd = turns.build_cmd("/bin/claude", mode="skip", prompt="hi")
    assert "--dangerously-skip-permissions" in cmd
    assert "--permission-prompt-tool" not in cmd


def test_parse_stream_reads_session_and_result():
    stdout = "\n".join(json.dumps(o) for o in [
        {"type": "system", "subtype": "init", "session_id": "sess-9"},
        {"type": "assistant", "message": {"content": [{"type": "text", "text": "hi"}]}},
        {"type": "result", "subtype": "success", "is_error": False, "result": "done"},
    ])
    assert turns.parse_stream(stdout) == ("sess-9", "done", False)


def test_parse_stream_without_a_result_is_an_error():
    session_id, text, is_error = turns.parse_stream(
        json.dumps({"type": "system", "session_id": "s"}))
    assert (session_id, is_error) == ("s", True)
    assert text == ""


def test_activity_line_prefers_the_tool():
    assert turns.activity_line({
        "type": "assistant",
        "message": {"content": [{"type": "tool_use", "name": "Bash"}]},
    }) == "⚙ Bash"
    assert turns.activity_line({"type": "result"}) is None


def test_context_window_tracks_the_1m_models():
    assert turns.context_window_for("claude-opus-4-8[1m]") == 1_000_000
    assert turns.context_window_for("claude-opus-4-8") == 200_000


# ---------------------------------------------------------------------------
# the rail
# ---------------------------------------------------------------------------

def make_rail(**kwargs):
    """A rail whose turns never spawn anything — the runner is a stub."""
    calls: list[dict] = []

    def runner(config, **turn):
        calls.append(turn)
        for _ in range(2):
            if turn.get("on_activity"):
                turn["on_activity"]("⚙ Read")
        if turn.get("on_session"):
            turn["on_session"]("sess-1")
        if turn.get("on_event"):
            turn["on_event"]({"type": "result",
                              "usage": {"input_tokens": 1000,
                                        "cache_read_input_tokens": 120_000}})
        return TurnResult(True, "sess-1", "the answer")

    config = dict(store.CONFIG_DEFAULTS)
    config.update(kwargs)
    rail = ChatRail(config, claude_bin="/bin/claude", turn_runner=runner)
    _RAILS.append(rail)
    return rail, calls


def test_a_new_conversation_boots_in_the_named_workspace(workdir):
    store.add_workspace(workdir, workspace_id="proj", name="Project")
    rail, calls = make_rail()

    result = rail.post_operator_message(None, "hello", workspace="proj")
    assert result["ok"] is True
    conv_id = result["conversation_id"]
    assert wait_for(lambda: rail.status_of(conv_id) == "idle" and calls)

    conv = store.load_conversation(conv_id)
    assert conv["cwd"] == str(workdir)
    assert conv["workspace"] == "proj"
    assert calls[0]["cwd"] == str(workdir)
    assert [m["role"] for m in conv["messages"]] == ["operator", "system", "assistant"]
    assert conv["messages"][-1]["text"] == "the answer"
    assert conv["context_tokens"] == 121_000


def test_at_shorthand_picks_the_workspace_and_leaves_the_message_clean(workdir):
    store.add_workspace(workdir, workspace_id="proj")
    rail, calls = make_rail()

    conv_id = rail.post_operator_message(None, "@proj run the tests")["conversation_id"]
    assert wait_for(lambda: calls)
    conv = store.load_conversation(conv_id)
    assert conv["workspace"] == "proj"
    assert conv["messages"][0]["text"] == "run the tests"
    assert calls[0]["text"] == "run the tests"


def test_an_unmatched_at_word_is_left_in_the_message(workdir):
    config_dir = store.add_workspace(workdir, workspace_id="proj")
    assert config_dir  # registered, but the message names something else
    rail, calls = make_rail()

    conv_id = rail.post_operator_message(None, "@someone can you look")["conversation_id"]
    assert wait_for(lambda: calls)
    conv = store.load_conversation(conv_id)
    assert conv["workspace"] == ""
    assert conv["messages"][0]["text"] == "@someone can you look"


def test_an_unknown_workspace_is_refused():
    rail, _ = make_rail()
    result = rail.post_operator_message(None, "hi", workspace="ghost")
    assert result == {"ok": False, "reason": "unknown workspace 'ghost'"}


def test_a_path_that_is_not_a_directory_is_refused(tmp_path):
    rail, calls = make_rail()
    result = rail.post_operator_message(None, "hi", cwd=str(tmp_path / "nope"))
    assert result["ok"] is False
    assert "not a directory" in result["reason"]
    assert not calls   # nothing spawned — fail before the turn, not during it


def test_an_ad_hoc_path_pins_the_conversation(workdir):
    rail, calls = make_rail()
    conv_id = rail.post_operator_message(None, "hi", cwd=str(workdir))["conversation_id"]
    assert wait_for(lambda: calls)
    assert store.load_conversation(conv_id)["cwd"] == str(workdir)
    assert store.load_conversation(conv_id)["workspace"] == ""


def test_a_reply_resumes_the_recorded_session(workdir):
    rail, calls = make_rail()
    conv_id = rail.post_operator_message(None, "first", cwd=str(workdir))["conversation_id"]
    assert wait_for(lambda: len(calls) == 1 and rail.status_of(conv_id) == "idle")

    rail.post_operator_message(conv_id, "second")
    assert wait_for(lambda: len(calls) == 2)
    assert calls[0]["resume"] is None
    assert calls[1]["resume"] == "sess-1"
    assert calls[1]["cwd"] == str(workdir)   # a resumed session cannot move


def test_the_workspaces_model_and_boot_win_over_the_daemon_default(workdir):
    store.add_workspace(workdir, workspace_id="proj", model="haiku", mode="skip",
                        boot="/warmup", dirs=[str(workdir)])
    seen: dict = {}

    def runner(config, **turn):
        seen.update(turn)
        return TurnResult(True, "s", "ok")

    rail = ChatRail(dict(store.CONFIG_DEFAULTS, model="opus"),
                    claude_bin="/bin/claude", turn_runner=runner)
    rail.post_operator_message(None, "go", workspace="proj")
    assert wait_for(lambda: "workspace" in seen)
    assert seen["workspace"]["model"] == "haiku"
    assert seen["workspace"]["boot"] == "/warmup"
    assert seen["workspace"]["mode"] == "skip"


def test_say_posts_the_sessions_mid_turn_voice(workdir):
    rail, _ = make_rail()
    conv_id = rail.post_operator_message(None, "hi", cwd=str(workdir))["conversation_id"]
    assert wait_for(lambda: rail.status_of(conv_id) == "idle")

    assert rail.say(conv_id, "still working on it")["ok"] is True
    kinds = [(m["role"], m["kind"]) for m in store.load_conversation(conv_id)["messages"]]
    assert ("assistant", "say") in kinds
    assert rail.say("nope", "x")["ok"] is False


def test_a_failed_turn_reports_instead_of_faking_an_answer(workdir):
    def runner(config, **turn):
        return TurnResult(False, None, "", "exit 1: boom")

    rail = ChatRail(dict(store.CONFIG_DEFAULTS), claude_bin="/bin/claude",
                    turn_runner=runner)
    conv_id = rail.post_operator_message(None, "hi", cwd=str(workdir))["conversation_id"]
    assert wait_for(lambda: rail.status_of(conv_id) == "idle")
    last = store.load_conversation(conv_id)["messages"][-1]
    assert (last["role"], last["kind"]) == ("system", "error")
    assert "boom" in last["text"]


def test_delete_removes_the_conversation_and_its_thread(workdir):
    rail, _ = make_rail()
    conv_id = rail.post_operator_message(None, "hi", cwd=str(workdir))["conversation_id"]
    assert wait_for(lambda: rail.status_of(conv_id) == "idle")

    assert rail.delete(conv_id)["ok"] is True
    assert store.load_conversation(conv_id) is None
    assert conv_id not in store.load_threads()
    assert rail.delete(conv_id)["ok"] is False


def test_session_announce_binds_a_relay_title(workdir, monkeypatch):
    """Headless sessions never self-register with the relay; the daemon must
    bind the announced session id to a stable title, or relay_steer can
    never find the session and every mid-turn reply queues."""
    bound = []
    monkeypatch.setattr(turns, "relay_register",
                        lambda sid, title: bound.append((sid, title)) or True)
    rail, _ = make_rail()
    conv_id = rail.post_operator_message(None, "hi", cwd=str(workdir))["conversation_id"]
    assert wait_for(lambda: rail.status_of(conv_id) == "idle")
    assert bound == [("sess-1", f"cc-{conv_id}")]


def test_relay_register_shells_the_bind_and_survives_no_base(monkeypatch):
    calls = []

    class Done:
        returncode = 0

    monkeypatch.setattr(turns, "find_base", lambda: "/usr/bin/base")
    monkeypatch.setattr(turns.subprocess, "run",
                        lambda argv, **kw: calls.append(argv) or Done())
    assert _REAL_RELAY_REGISTER("sess-1", "cc-c9") is True
    assert calls[0][1:] == ["relay", "register", "--as", "cc-c9",
                            "--session", "sess-1"]
    monkeypatch.setattr(turns, "find_base", lambda: None)
    assert _REAL_RELAY_REGISTER("sess-1", "cc-c9") is False


def test_update_setting_changes_mode_and_model_live():
    rail, _ = make_rail(model="claude-opus-4-8")

    assert rail.update_setting("mode", "skip") == {"ok": True, "mode": "skip"}
    assert rail.config["mode"] == "skip"                  # live, for the next turn
    assert store.load_config()["mode"] == "skip"          # and persisted

    assert rail.update_setting("model", "claude-sonnet-5")["model"] == "claude-sonnet-5"
    assert rail.state_payload()["model"] == "claude-sonnet-5"
    # empty clears back to the account default
    assert rail.update_setting("model", "")["model"] == ""


def test_update_setting_refuses_bad_input():
    rail, _ = make_rail()
    assert rail.update_setting("mode", "yolo")["ok"] is False
    assert rail.update_setting("port", 9000)["ok"] is False       # not a UI setting
    assert rail.update_setting("host", "0.0.0.0")["ok"] is False  # network stays CLI-only
    assert rail.config["mode"] == "approve"                       # unchanged


def test_update_setting_toggles_the_behavior_booleans():
    rail, _ = make_rail()
    assert rail.update_setting("ack_posts", False)["ack_posts"] is False
    assert rail.update_setting("midflight_relay", False)["midflight_relay"] is False
    assert rail.config["ack_posts"] is False
    snap = rail._settings_snapshot()
    assert snap["ack_posts"] is False and snap["midflight_relay"] is False


def test_default_workspace_setting_must_name_a_real_workspace(workdir):
    rail, _ = make_rail()
    assert rail.update_setting("default_workspace", "ghost")["ok"] is False
    store.add_workspace(workdir, workspace_id="proj")
    assert rail.update_setting("default_workspace", "proj")["ok"] is True
    assert rail.config["default_workspace"] == "proj"


def test_default_cwd_setting_must_be_a_directory(tmp_path):
    rail, _ = make_rail()
    assert rail.update_setting("default_cwd", str(tmp_path / "nope"))["ok"] is False
    assert rail.update_setting("default_cwd", str(tmp_path))["ok"] is True


def test_save_workspace_over_the_rail(workdir):
    rail, _ = make_rail()
    r = rail.save_workspace({"path": str(workdir), "id": "proj", "name": "Project",
                             "model": "claude-sonnet-5", "mode": "skip",
                             "boot": "/warmup", "dirs": [str(workdir)],
                             "default": True})
    assert r["ok"] is True
    entry = store.find_workspace("proj")
    assert entry["model"] == "claude-sonnet-5"
    assert entry["mode"] == "skip"
    assert entry["boot"] == "/warmup"
    assert rail.config["default_workspace"] == "proj"       # --default took


def test_save_workspace_validates_path_and_mode(workdir, tmp_path):
    rail, _ = make_rail()
    assert rail.save_workspace({"path": ""})["ok"] is False
    assert rail.save_workspace({"path": str(tmp_path / "gone")})["ok"] is False
    assert rail.save_workspace({"path": str(workdir), "mode": "loose"})["ok"] is False


def test_save_workspace_broadcasts_for_the_picker(workdir):
    rail, _ = make_rail()
    before = rail.bus.seq
    rail.save_workspace({"path": str(workdir), "id": "proj"})
    kinds = {kind for _, kind, _ in rail.bus.wait_since(before, timeout=0.1)}
    assert "workspaces" in kinds


def test_remove_workspace_over_the_rail_clears_the_default(workdir):
    rail, _ = make_rail()
    rail.save_workspace({"path": str(workdir), "id": "proj", "default": True})
    assert rail.config["default_workspace"] == "proj"
    assert rail.remove_workspace("proj")["ok"] is True
    assert store.find_workspace("proj") is None
    assert rail.config["default_workspace"] == ""           # live copy re-synced
    assert rail.remove_workspace("proj")["ok"] is False


def test_state_payload_exposes_the_settings_snapshot_and_connection():
    rail, _ = make_rail(hostname="chat.go", host="127.0.0.1", port=7788)
    payload = rail.state_payload()
    assert set(payload["settings"]) >= {"mode", "model", "ack_posts",
                                        "midflight_relay", "full_load",
                                        "default_workspace", "default_cwd"}
    assert payload["connection"]["hostname"] == "chat.go"
    assert payload["connection"]["port"] == 7788


def test_update_setting_broadcasts_so_open_clients_refresh():
    rail, _ = make_rail()
    before = rail.bus.seq
    rail.update_setting("mode", "skip")
    events = rail.bus.wait_since(before, timeout=0.1)
    kinds = {kind: data for _, kind, data in events}
    assert "settings" in kinds
    assert kinds["settings"]["mode"] == "skip"


def test_a_live_mode_flip_reaches_the_next_turn(workdir):
    """The pill is only real if the change actually alters the spawned turn —
    flip to skip and the next turn must run in skip, not approve."""
    seen: list[str] = []

    def runner(config, **turn):
        seen.append(str((turn.get("workspace") or {}).get("mode")
                        or config.get("mode")))
        return TurnResult(True, "s", "ok")

    rail = ChatRail(dict(store.CONFIG_DEFAULTS, mode="approve"),
                    claude_bin="/bin/claude", turn_runner=runner)
    _RAILS.append(rail)
    cid = rail.post_operator_message(None, "one", cwd=str(workdir))["conversation_id"]
    assert wait_for(lambda: len(seen) == 1)
    rail.update_setting("mode", "skip")
    rail.post_operator_message(cid, "two")
    assert wait_for(lambda: len(seen) == 2)
    assert seen == ["approve", "skip"]


def test_state_payload_carries_the_workspaces(workdir):
    store.add_workspace(workdir, workspace_id="proj")
    rail, _ = make_rail(default_workspace="proj")
    payload = rail.state_payload()
    assert [w["id"] for w in payload["workspaces"]] == ["proj"]
    assert payload["default_workspace"] == "proj"


# ---------------------------------------------------------------------------
# approvals — the gate is fail-closed by construction
# ---------------------------------------------------------------------------

def test_approval_allow_then_first_verdict_wins(workdir):
    rail, _ = make_rail()
    conv_id = rail.post_operator_message(None, "hi", cwd=str(workdir))["conversation_id"]
    assert wait_for(lambda: rail.status_of(conv_id) == "idle")

    created = rail.create_approval(conv_id, "Bash", {"command": "ls"}, 300)
    request_id = created["id"]
    assert rail.approval_state(request_id)["verdict"] is None

    assert rail.set_verdict(request_id, "allow")["verdict"] == "allow"
    # a late deny must not flip an answered card
    assert rail.set_verdict(request_id, "deny")["verdict"] == "allow"

    card = [m for m in store.load_conversation(conv_id)["messages"]
            if m.get("approval_id") == request_id][0]
    assert card["verdict"] == "allow"
    assert card["tool_name"] == "Bash"


def test_approval_rejects_a_bogus_verdict_and_an_unknown_id():
    rail, _ = make_rail()
    assert rail.set_verdict("nope", "allow")["ok"] is False
    assert rail.create_approval("nope", "Bash", {}, 300)["ok"] is False


def test_approval_detail_is_truncated(workdir):
    rail, _ = make_rail()
    conv_id = rail.post_operator_message(None, "hi", cwd=str(workdir))["conversation_id"]
    assert wait_for(lambda: rail.status_of(conv_id) == "idle")

    request_id = rail.create_approval(conv_id, "Write", {"content": "x" * 5000}, 300)["id"]
    card = [m for m in store.load_conversation(conv_id)["messages"]
            if m.get("approval_id") == request_id][0]
    assert len(card["detail"]) < 700
    assert card["detail"].endswith("(truncated)")


# ---------------------------------------------------------------------------
# the permission gate — the MCP bridge Claude Code actually talks to
# ---------------------------------------------------------------------------

def call_gate(**tool_args):
    """Drive the real MCP tool and hand back (content blocks, structuredContent)."""
    import asyncio

    from claude_chat.approve import build_server
    result = asyncio.run(build_server().call_tool(
        "approve", {"tool_name": "Write", "input": {}, **tool_args}))
    return result if isinstance(result, tuple) else (result, None)


def test_the_gate_puts_exactly_one_text_block_on_the_wire(monkeypatch):
    """Claude Code rejects a permission-prompt result that is anything other
    than a single text block — a typed return makes the MCP SDK attach
    `structuredContent` too, and EVERY gated call then dies at the bridge.
    This is the test that catches that regression; it is not cosmetic."""
    monkeypatch.delenv("CLAUDE_CHAT_URL", raising=False)
    content, structured = call_gate()

    assert len(content) == 1
    assert content[0].type == "text"
    assert structured in (None, {})
    assert isinstance(json.loads(content[0].text)["behavior"], str)


def test_the_gate_denies_when_it_is_not_configured(monkeypatch):
    monkeypatch.delenv("CLAUDE_CHAT_URL", raising=False)
    monkeypatch.delenv("CLAUDE_CHAT_CONVERSATION", raising=False)
    content, _ = call_gate()
    verdict = json.loads(content[0].text)
    assert verdict["behavior"] == "deny"
    assert "misconfigured" in verdict["message"]


def test_the_gate_denies_when_the_daemon_is_unreachable(monkeypatch):
    from claude_chat import approve

    monkeypatch.setenv("CLAUDE_CHAT_URL", "http://127.0.0.1:1")
    monkeypatch.setenv("CLAUDE_CHAT_CONVERSATION", "c1")
    monkeypatch.setattr(approve, "_chat_call", lambda *a, **k: {"ok": False})
    verdict = json.loads(approve.decide("Write", {"file_path": "/tmp/x"}))
    assert verdict["behavior"] == "deny"


def test_the_gate_allows_and_echoes_the_input_back(monkeypatch):
    from claude_chat import approve

    monkeypatch.setenv("CLAUDE_CHAT_URL", "http://daemon")
    monkeypatch.setenv("CLAUDE_CHAT_CONVERSATION", "c1")
    monkeypatch.setattr(approve, "_chat_call", lambda url, payload=None, **k:
                        {"ok": True, "id": "a1"} if payload else
                        {"ok": True, "verdict": "allow"})
    verdict = json.loads(approve.decide("Write", {"file_path": "/tmp/x"}))
    assert verdict == {"behavior": "allow", "updatedInput": {"file_path": "/tmp/x"}}


def test_the_gate_denies_on_an_unanswered_card(monkeypatch):
    from claude_chat import approve

    monkeypatch.setenv("CLAUDE_CHAT_URL", "http://daemon")
    monkeypatch.setenv("CLAUDE_CHAT_CONVERSATION", "c1")
    monkeypatch.setenv("CLAUDE_CHAT_APPROVE_TIMEOUT", "0")   # deadline already passed
    monkeypatch.setattr(approve, "_chat_call", lambda url, payload=None, **k:
                        {"ok": True, "id": "a1"} if payload else
                        {"ok": True, "verdict": None})
    verdict = json.loads(approve.decide("Write", {}))
    assert verdict["behavior"] == "deny"
    assert "did not answer" in verdict["message"]


# ---------------------------------------------------------------------------
# hostname wiring
# ---------------------------------------------------------------------------

def test_valid_hostname():
    assert hosts.valid_hostname("chat.go")
    assert not hosts.valid_hostname("chat go")
    assert not hosts.valid_hostname("-chat.go")


def test_rewrite_fences_its_block_and_leaves_the_rest_alone(tmp_path):
    path = tmp_path / "hosts"
    path.write_text("127.0.0.1\tlocalhost\n10.0.0.1 other.host\n", encoding="utf-8")

    assert hosts._rewrite(path, ["chat.go"], "172.20.0.2", crlf=False) is True
    text = path.read_text(encoding="utf-8")
    assert "127.0.0.1\tlocalhost" in text
    assert "10.0.0.1 other.host" in text
    assert "172.20.0.2 chat.go" in text

    # idempotent: the same call again is a no-op
    assert hosts._rewrite(path, ["chat.go"], "172.20.0.2", crlf=False) is False

    # a moved IP replaces the block, never appends a second one
    assert hosts._rewrite(path, ["chat.go"], "172.20.9.9", crlf=False) is True
    assert path.read_text(encoding="utf-8").count("chat.go") == 1

    # an empty name list removes the block entirely
    assert hosts._rewrite(path, [], "", crlf=False) is True
    final = path.read_text(encoding="utf-8")
    assert "chat.go" not in final and hosts.MARKER not in final
    assert "10.0.0.1 other.host" in final


def test_vhost_conf_never_buffers_the_event_stream():
    target = "http://127.0.0.1:7788"
    assert "flushpackets=on" in hosts.vhost_conf("apache2", ["chat.go"], target)
    assert "proxy_buffering off" in hosts.vhost_conf("nginx", ["chat.go"], target)
    assert "flush_interval -1" in hosts.vhost_conf("caddy", ["chat.go"], target)
    for proxy in ("apache2", "nginx", "caddy"):
        assert target in hosts.vhost_conf(proxy, ["chat.go"], target)


def test_install_proxy_refuses_without_root(monkeypatch):
    monkeypatch.setattr(hosts.os, "geteuid", lambda: 1000)
    result = hosts.install_proxy(["chat.go"], "http://127.0.0.1:7788")
    assert result["ok"] is False
    assert "sudo" in result["reason"]
