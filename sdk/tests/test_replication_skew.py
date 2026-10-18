"""The skew-guard helper itself (decision 54447516, #216), on synthetic
inventories: one passing, one with an undeclared key, one with a stale
declaration, one with an unguarded widened key."""

from __future__ import annotations

import pytest
from snowline_plugin_sdk.replication.skew import (
    DECISION,
    KeyInventory,
    check_emitted_keys,
    check_emitted_payloads,
    find_unguarded_widened_keys,
)

INV = KeyInventory(
    payload_keys={"thing.set": {"id", "name"}, "thing.gone": {"id"}},
    widened_keys={"thing.set": {"color"}},
)

GUARDED = '''
def apply(p):
    row.name = p["name"]
    if "color" in p:
        row.color = p["color"]
'''


def test_matching_payload_is_ok():
    assert check_emitted_keys(INV, "thing.set", {"id": 1, "name": "a", "color": "r"}) == []
    assert check_emitted_payloads(
        INV, {"thing.set": [{"id": 1, "name": "a", "color": "r"}], "thing.gone": [{"id": 1}]}
    ) == []


def test_undeclared_key_is_a_silent_widening():
    out = check_emitted_keys(
        INV, "thing.set", {"id": 1, "name": "a", "color": "r", "size": 3}
    )
    assert len(out) == 1 and "`size`" in out[0] and DECISION in out[0]
    assert "WIDENED_KEYS" in out[0]


def test_stale_declaration_is_reported():
    out = check_emitted_keys(INV, "thing.set", {"id": 1, "color": "r"})
    assert len(out) == 1 and "`name`" in out[0] and "stale" in out[0]
    assert DECISION in out[0]


def test_undeclared_event_type_and_never_emitted_type():
    assert "no declaration" in check_emitted_keys(INV, "other.x", {})[0]
    out = check_emitted_payloads(INV, {"thing.set": [{"id": 1, "name": "a", "color": "r"}]})
    assert len(out) == 1 and "thing.gone" in out[0] and "never emitted" in out[0]
    assert check_emitted_payloads(
        INV, {"thing.set": [{"id": 1, "name": "a", "color": "r"}]}, require_all_types=False
    ) == []


def test_guarded_widened_key_passes():
    assert find_unguarded_widened_keys(INV, GUARDED) == []
    assert find_unguarded_widened_keys(INV, 'x = p.get("color")') == []
    assert find_unguarded_widened_keys(INV, 'if "color" not in payload: pass') == []
    assert find_unguarded_widened_keys(INV, 'stamp(payload, "color")') == []


def test_unguarded_widened_key_is_flagged():
    out = find_unguarded_widened_keys(INV, 'def apply(p):\n    row.color = p["color"]\n')
    assert len(out) == 1 and "`color`" in out[0] and DECISION in out[0]
    # an unrelated literal next to a non-payload name is not a guard
    assert find_unguarded_widened_keys(INV, 'f(other, "color")')


def test_empty_widened_is_armed_but_trivially_ok():
    inv = KeyInventory(payload_keys={"a.b": {"id"}}, widened_keys={})
    assert inv.widened_keys == {"a.b": frozenset()}
    assert find_unguarded_widened_keys(inv, "pass") == []


def test_inventory_rejects_overlap_and_unknown_types():
    with pytest.raises(ValueError, match="BOTH"):
        KeyInventory(payload_keys={"a": {"x"}}, widened_keys={"a": {"x"}})
    with pytest.raises(ValueError, match="absent"):
        KeyInventory(payload_keys={"a": {"x"}}, widened_keys={"b": {"y"}})


OPT = KeyInventory(
    payload_keys={"thing.set": {"id"}},
    widened_keys={"thing.set": {"color"}},
    optional_keys={"thing.set": {"author"}},
)


def test_optional_key_is_allowed_when_present_and_not_required():
    base = {"id": 1, "color": "r"}
    assert check_emitted_keys(OPT, "thing.set", base) == []  # absent: not stale
    assert check_emitted_keys(OPT, "thing.set", {**base, "author": "x"}) == []
    out = check_emitted_keys(OPT, "thing.set", {**base, "other": 1})
    assert len(out) == 1 and "`other`" in out[0]
    assert check_emitted_payloads(OPT, {"thing.set": [base, {**base, "author": "x"}]}) == []


def test_optional_key_overlap_is_rejected():
    with pytest.raises(ValueError, match="payload_keys and optional_keys"):
        KeyInventory(payload_keys={"a": {"x"}}, optional_keys={"a": {"x"}})
    with pytest.raises(ValueError, match="widened_keys and optional_keys"):
        KeyInventory(
            payload_keys={"a": {"x"}}, widened_keys={"a": {"y"}}, optional_keys={"a": {"y"}}
        )
    with pytest.raises(ValueError, match="absent"):
        KeyInventory(payload_keys={"a": {"x"}}, optional_keys={"b": {"y"}})


def test_optional_key_needs_an_apply_guard_too():
    src = 'if "color" in p: pass'
    out = find_unguarded_widened_keys(OPT, src)
    assert len(out) == 1 and "`author`" in out[0]
    assert find_unguarded_widened_keys(OPT, src + '\nz = p.get("author")') == []


def test_get_on_a_non_payload_name_does_not_count():
    out = find_unguarded_widened_keys(INV, 'x = settings.get("color")\n"color" in other')
    assert len(out) == 1 and "`color`" in out[0]
    assert find_unguarded_widened_keys(INV, 'x = envelope["payload"].get("color")')
    assert (
        find_unguarded_widened_keys(INV, 'x = row.get("color")', payload_names=("row",))
        == []
    )
