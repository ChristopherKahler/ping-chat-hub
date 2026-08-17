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
