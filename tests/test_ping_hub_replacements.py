"""The word-replacement store.

The whole value of this feature is that there is ONE list. So the tests that
matter are the ones about identity with cx-ptt's matching and about the
migration leaving exactly one place to edit — not about JSON round-tripping.

Everything runs against a scratch store; `conftest.py` has already pinned the
config away from the real one.
"""

from __future__ import annotations

import json
import re

import pytest

from ping_hub import replacements as rep
from ping_hub.config import Config

from test_ping_hub_config import StubProbe


@pytest.fixture
def cfg(tmp_path) -> Config:
    return Config({"paths": {"base_gbl": str(tmp_path / "gbl")}}, probe=StubProbe())


def _cx_toml(tmp_path, body: str) -> Config:
    p = tmp_path / "cx.toml"
    p.write_text(body, encoding="utf-8")
    return Config({"paths": {"base_gbl": str(tmp_path / "gbl")},
                   "cx_ptt": {"cx_toml": str(p)}}, probe=StubProbe())


# ── matching is cx-ptt's, exactly ────────────────────────────────────────────
def cx_ptt_reference(text: str, table: dict) -> str:
    """cx-ptt.py:172, copied verbatim. If these two ever disagree, the same
    sentence comes out differently depending on which microphone was used."""
    for wrong, right in table.items():
        text = re.sub(r"(?i)\b" + re.escape(wrong) + r"\b", right, text)
    return text


@pytest.mark.parametrize("said", [
    "bayant said hello",
    "BAYANT SHOUTED",
    "we use tell scale for the phone",
    "tell-scale and telscale both",
    "nothing to change here",
    "bayanted is a different word",      # \b must not match inside a word
    "punctuation: bayant, then bayant.",
])
def test_matches_cx_ptt_byte_for_byte(cfg, said):
    table = {"bayant": "Beant", "tell scale": "tailscale",
             "tell-scale": "tailscale", "telscale": "tailscale"}
    pairs = [{"from": k, "to": v, "enabled": True} for k, v in table.items()]
    assert rep.apply(said, pairs) == cx_ptt_reference(said, table)


def test_order_is_behaviour_not_an_implementation_detail():
    """Overlapping rules resolve by position, which is why the store is a LIST.
    A mapping would make this depend on insertion luck."""
    forward = [{"from": "a b", "to": "X", "enabled": True},
               {"from": "b", "to": "Y", "enabled": True}]
    assert rep.apply("a b", forward) == "X"
    assert rep.apply("a b", list(reversed(forward))) == "a Y"


def test_a_disabled_pair_is_kept_but_not_applied():
    pairs = [{"from": "foo", "to": "bar", "enabled": False}]
    assert rep.apply("foo", pairs) == "foo"


def test_regex_characters_in_a_pair_are_literal():
    """Someone will type a '.' or a '(' eventually; it must not become a
    wildcard that rewrites unrelated words."""
    pairs = [{"from": "c.a.t", "to": "cat", "enabled": True}]
    assert rep.apply("c.a.t", pairs) == "cat"
    assert rep.apply("czamtz", pairs) == "czamtz"


def test_an_empty_left_side_matches_nothing():
    assert rep.apply("untouched", [{"from": "", "to": "X"}]) == "untouched"


# ── the store ────────────────────────────────────────────────────────────────
def test_absent_store_is_empty_not_an_error(cfg):
    doc = rep.load(cfg)
    assert doc["pairs"] == [] and doc["version"] == rep.VERSION


def test_a_corrupt_store_reads_as_empty_rather_than_crashing_the_mic(cfg):
    p = rep.store_path(cfg)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("{ this is not json", encoding="utf-8")
    assert rep.load(cfg)["pairs"] == []


def test_save_then_load_round_trips(cfg):
    rep.save(cfg, {"version": 1, "imported_from_cx_toml": True,
                   "pairs": [{"from": "a", "to": "b", "enabled": True}]})
    assert rep.load(cfg)["pairs"] == [{"from": "a", "to": "b", "enabled": True}]


def test_save_is_atomic_and_leaves_no_partial_file(cfg):
    """cx-ptt polls this file; a half-written store must never be visible."""
    rep.save(cfg, {"pairs": [{"from": "a", "to": "b"}]})
    assert rep.store_path(cfg).is_file()
    assert not list(rep.store_path(cfg).parent.glob("*.tmp"))


def test_normalise_drops_rules_that_cannot_match(cfg):
    got = rep.normalise([{"from": " x ", "to": "y"}, {"from": "", "to": "z"},
                         "not a dict", {"to": "no left side"}])
    assert got == [{"from": "x", "to": "y", "enabled": True}]


# ── migration: one place to edit, not two ────────────────────────────────────
CX_BODY = """\
[settings]
debug_keys = true

[settings.replacements]
"bayant" = "Beant"
"tell scale" = "tailscale"
"""


def test_migration_imports_cx_toml_pairs_in_file_order(tmp_path):
    c = _cx_toml(tmp_path, CX_BODY)
    doc = rep.migrate_if_needed(c)
    assert [p["from"] for p in doc["pairs"]] == ["bayant", "tell scale"]
    assert doc["imported_from_cx_toml"] is True


def test_migration_happens_exactly_once(tmp_path):
    c = _cx_toml(tmp_path, CX_BODY)
    rep.migrate_if_needed(c)
    doc = rep.load(c)
    doc["pairs"] = [p for p in doc["pairs"] if p["from"] != "bayant"]
    rep.save(c, doc)
    # the deleted pair must NOT come back on the next call
    again = rep.migrate_if_needed(c)
    assert [p["from"] for p in again["pairs"]] == ["tell scale"]


def test_deleting_every_pair_does_not_re_trigger_the_import(tmp_path):
    """The hole in the layered design: with a merge, deleting a pair left it
    firing from cx.toml forever. Import-once makes delete mean delete."""
    c = _cx_toml(tmp_path, CX_BODY)
    rep.migrate_if_needed(c)
    doc = rep.load(c)
    doc["pairs"] = []
    rep.save(c, doc)
    assert rep.migrate_if_needed(c)["pairs"] == []
    assert rep.apply_for(c, "bayant") == "bayant"


def test_no_cx_toml_is_a_clean_empty_start(tmp_path):
    """Albert's machine: no cx-ptt, so one consumer and nothing to import."""
    c = Config({"paths": {"base_gbl": str(tmp_path / "gbl")},
                "cx_ptt": {"cx_toml": str(tmp_path / "nope.toml")}},
               probe=StubProbe())
    doc = rep.migrate_if_needed(c)
    assert doc["pairs"] == [] and doc["imported_from_cx_toml"] is True


def test_the_store_never_writes_cx_toml(tmp_path):
    """The ruling: one writer per file. cx.toml belongs to cx-ptt."""
    c = _cx_toml(tmp_path, CX_BODY)
    before = (tmp_path / "cx.toml").read_bytes()
    rep.migrate_if_needed(c)
    rep.save(c, {"pairs": [{"from": "new", "to": "pair", "enabled": True}]})
    assert (tmp_path / "cx.toml").read_bytes() == before


def test_apply_for_uses_the_stored_pairs(cfg):
    rep.save(cfg, {"pairs": [{"from": "tell scale", "to": "tailscale",
                              "enabled": True}]})
    assert rep.apply_for(cfg, "use tell scale") == "use tailscale"


def test_the_store_is_json_cx_ptt_can_read(cfg):
    rep.save(cfg, {"pairs": [{"from": "a", "to": "b", "enabled": True}]})
    doc = json.loads(rep.store_path(cfg).read_text(encoding="utf-8"))
    assert doc["pairs"][0]["from"] == "a"
    assert set(doc) >= {"version", "pairs"}


# ── star-commands as spoken fixes: generation ────────────────────────────────
@pytest.mark.parametrize("name,heard,sent", [
    ("end", "star end", "*end"),
    ("discuss", "star discuss", "*discuss"),
    ("map-codebase", "star map codebase", "*map-codebase"),
    ("map_codebase", "star map codebase", "*map_codebase"),
    ("a-b_c", "star a b c", "*a-b_c"),
])
def test_a_command_is_heard_with_its_separators_spoken_as_spaces(name, heard, sent):
    p = rep.generate_pair(name)
    assert (p["from"], p["to"]) == (heard, sent)
    assert p["enabled"] is True and p["origin"] == rep.ORIGIN_IMPORT


@pytest.mark.parametrize("name", ["", " ", "   ", "-", "_", "--__", "\t"])
def test_a_name_that_would_leave_a_bare_star_is_skipped(name):
    """This is what "a blank left side" really means here. With a "star "
    prefix the left can never be empty — it collapses to the bare word `star`,
    and a rule rewriting the standalone word "star" corrupts every transcript.
    The command feed does not catch it: its filter is `if c.get("name")`, and a
    single space is truthy."""
    assert rep.generate_pair(name) is None
    assert rep.command_pairs([name]) == []


def test_no_generated_rule_is_ever_the_bare_word_star():
    pairs = rep.command_pairs(["end", " ", "-", "discuss", ""])
    assert [p["from"] for p in pairs] == ["star end", "star discuss"]
    assert all(p["from"] != rep.STAR_WORD for p in pairs)


def test_a_filtered_subset_is_a_strict_subset_of_import_all():
    """The UI filter narrows what is SHOWN. Names are sent explicitly, so a
    filtered import can only ever be a subset of import-all — the filter
    cannot change what import-all would do."""
    everything = {p["from"] for p in rep.command_pairs(["end", "discuss", "fork"])}
    filtered = {p["from"] for p in rep.command_pairs(["discuss"])}
    assert filtered < everything


# ── star-commands as spoken fixes: importing ─────────────────────────────────
def test_importing_twice_changes_nothing_the_second_time(cfg):
    first = rep.import_commands(cfg, [], ["end", "discuss"])
    assert (first["added"], first["skipped"]) == (2, 0)
    second = rep.import_commands(cfg, first["pairs"], ["end", "discuss"])
    assert (second["added"], second["skipped"]) == (0, 2)
    assert second["pairs"] == first["pairs"]


def test_a_hand_added_pair_survives_an_import_untouched(cfg):
    """Position, enabled state, and the absence of a marker all intact — an
    import adds rules, it never edits the ones already there."""
    hand = [{"from": "kaylor", "to": "kahler", "enabled": False},
            {"from": "tell scale", "to": "tailscale", "enabled": True}]
    out = rep.import_commands(cfg, hand, ["end"])
    assert out["pairs"][:2] == hand
    assert all("origin" not in p for p in out["pairs"][:2])
    assert out["pairs"][2]["from"] == "star end"


def test_an_existing_left_side_is_skipped_not_rewritten(cfg):
    """Skip, not upsert. Chris may have deliberately pointed `star end` at
    something else, or switched it off; an import must not undo that."""
    hand = [{"from": "star end", "to": "SOMETHING ELSE", "enabled": False}]
    out = rep.import_commands(cfg, hand, ["end"])
    assert out["pairs"] == hand
    assert (out["added"], out["skipped"]) == (0, 1)


@pytest.mark.parametrize("existing", ["Star End", "STAR  END", " star end "])
def test_the_duplicate_check_ignores_case_and_spacing(cfg, existing):
    """Matching is already case-insensitive, so two rules differing only in
    case are one rule — importing the second adds a duplicate that does
    nothing."""
    out = rep.import_commands(cfg, [{"from": existing, "to": "*end"}], ["end"])
    assert (out["added"], out["skipped"]) == (0, 1)


def test_no_names_imports_nothing_rather_than_everything(cfg):
    """A favourites import with no favourites. Empty is a real answer here,
    not a missing one."""
    out = rep.import_commands(cfg, [], [])
    assert out["pairs"] == [] and (out["added"], out["skipped"]) == (0, 0)


def test_generated_rules_append_behind_hand_written_ones(cfg):
    """Append, never prepend, proven through the transcript rather than the
    list order. A generic generated rule placed first eats the longer hand
    written one, and the loss is silent."""
    hand = [{"from": "star handoff to chris", "to": "*handoff chris",
             "enabled": True}]
    out = rep.import_commands(cfg, hand, ["handoff"])
    assert [p["from"] for p in out["pairs"]] == ["star handoff to chris",
                                                 "star handoff"]
    assert rep.apply("star handoff to chris", out["pairs"]) == "*handoff chris"
    assert rep.apply("star handoff now", out["pairs"]) == "*handoff now"
    # the same two rules the other way round lose the specific one entirely
    assert rep.apply("star handoff to chris",
                     list(reversed(out["pairs"]))) == "*handoff to chris"


def test_chris_existing_run_star_end_pair_still_wins_after_an_import(cfg):
    """His hand-written rule predates this feature and is live in the store
    today. Importing `end` adds the general rule behind it; both phrasings
    still come out right."""
    hand = [{"from": "run star end", "to": "run *end", "enabled": True}]
    out = rep.import_commands(cfg, hand, ["end"])
    assert rep.apply("run star end", out["pairs"]) == "run *end"
    assert rep.apply("star end", out["pairs"]) == "*end"


def test_an_import_is_saved_not_merely_returned(cfg):
    """S5 ruling: import commits. If it only returned the merged list, a
    dismissed modal would silently discard it."""
    rep.import_commands(cfg, [], ["end"])
    assert [p["from"] for p in rep.load(cfg)["pairs"]] == ["star end"]


# ── the origin marker ────────────────────────────────────────────────────────
def test_normalise_keeps_an_origin_only_when_there_is_one():
    """A hand-added pair is exactly {from,to,enabled} and stays that way. If
    the key were unconditional, every pair would claim an origin it does not
    have."""
    got = rep.normalise([{"from": "a", "to": "b"},
                         {"from": "c", "to": "d", "origin": "import"},
                         {"from": "e", "to": "f", "origin": "  "}])
    assert got == [{"from": "a", "to": "b", "enabled": True},
                   {"from": "c", "to": "d", "enabled": True, "origin": "import"},
                   {"from": "e", "to": "f", "enabled": True}]


def test_the_marker_survives_a_save_and_lands_only_on_generated_rows(cfg):
    rep.import_commands(cfg, [{"from": "kaylor", "to": "kahler"}], ["end"])
    pairs = rep.load(cfg)["pairs"]
    assert "origin" not in pairs[0]
    assert pairs[1]["origin"] == rep.ORIGIN_IMPORT


def test_migrated_cx_pairs_are_not_marked_as_imported_commands(tmp_path):
    """They are Chris's, carried over — not something this app generated. A
    future "remove imported" must not eat them."""
    c = _cx_toml(tmp_path, CX_BODY)
    doc = rep.migrate_if_needed(c)
    assert doc["pairs"] and all("origin" not in p for p in doc["pairs"])


def test_the_marker_does_not_change_what_cx_ptt_reads(cfg):
    """cx-ptt projects from/to/enabled and ignores the rest (cx-ptt.py:189).
    The marker must stay invisible to matching."""
    marked = [{"from": "star end", "to": "*end", "enabled": True,
               "origin": rep.ORIGIN_IMPORT}]
    plain = [{"from": "star end", "to": "*end", "enabled": True}]
    assert rep.apply("star end", marked) == rep.apply("star end", plain)


# ── the daemon wire ──────────────────────────────────────────────────────────
def test_read_commands_parses_the_toml_and_sorts_by_name():
    from ping_hub import daemon
    p = daemon.CFG.paths.base_gbl / "commands.toml"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text('[[command]]\nname = "zeta"\ndescription = "z"\n\n'
                 '[[command]]\nname = "alpha"\ndescription = "a"\n\n'
                 '[[command]]\ndescription = "nameless, dropped"\n',
                 encoding="utf-8")
    try:
        assert daemon.read_commands() == [{"name": "alpha", "description": "a"},
                                          {"name": "zeta", "description": "z"}]
    finally:
        p.unlink()


def test_the_import_endpoint_is_wired_and_reads_one_command_source():
    """The defect class here is an ABSENT wire, which no behaviour test can
    see — a function that is built and never called looks exactly like a
    function that works (the autostart lesson from the packaging run)."""
    from pathlib import Path

    from ping_hub import daemon
    src = Path(daemon.__file__).read_text(encoding="utf-8")
    assert '"/api/replacements/import"' in src
    assert "replacements.import_commands(" in src
    # one parse of the WSL-owned file, not two: the palette and the import
    # must agree on what a command is
    assert src.count('"commands.toml"') == 1


def test_the_daemon_never_writes_the_command_file():
    """commands.toml is base's, reached through a WSL-owned symlink. Read
    only, with no cache — a stale copy would import commands he has since
    renamed."""
    from pathlib import Path

    from ping_hub import daemon
    src = Path(daemon.__file__).read_text(encoding="utf-8")
    where = src.index('"commands.toml"')
    assert 'open(CFG.paths.base_gbl / "commands.toml", "rb")' in src
    assert '"w"' not in src[where - 120:where + 120]


# ── the UI wire ──────────────────────────────────────────────────────────────
def _hub_html() -> str:
    from ping_hub import daemon
    return daemon.HTML.read_text(encoding="utf-8")


def test_the_three_import_controls_exist_and_call_the_endpoint():
    """The same absent-wire class on the UI side: three buttons that look
    right and call nothing pass every server test in this file."""
    html = _hub_html()
    for el in ('id="rep-imp"', 'id="rep-all"', 'id="rep-fav"'):
        assert el in html
    assert '"/api/replacements/import"' in html
    # explicit names on the wire — never a magic "all" flag the server could
    # mis-read as "everything" when the real answer is "nothing"
    assert "JSON.stringify({ pairs, names })" in html
    # import-all reads the full command list, never the filtered view, so the
    # filter cannot change what it sends
    assert "importCommands(cmds.map(c => c.name)" in html


def test_the_import_ui_opens_no_browser_dialogs():
    """A modal dialog blocks the page and, on the phone, is unusable. The hub
    has its own overlay for this."""
    code = "\n".join(l for l in _hub_html().splitlines()
                     if "/*" not in l and not l.strip().startswith(("*", "//")))
    for dialog in ("confirm(", "alert(", "prompt("):
        assert dialog not in code
