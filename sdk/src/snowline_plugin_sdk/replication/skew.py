"""N-1 skew guard for replication payloads (decision 54447516, issue #216).

Decision 54447516 (N-1 train compatibility): every replication payload change is
ADDITIVE, and the apply side keeps a key-PRESENCE guard for at least one train
before the new key is treated as always-present. A peer one train behind keeps
emitting the old shape; an apply that reads the new key unconditionally nulls
the replica's already-set field on an LWW win.

This module is the shared, stdlib-only mechanism for enforcing that in tests —
a package declares a `KeyInventory` beside its `EVENT_TYPES` and wires the two
checks below into its own test suite:

  A. `check_emitted_keys` — driven by the package's full-write-surface emit
     test: every emitted payload's keys must equal the declared
     `payload_keys | widened_keys` for its event type (no undeclared key = no
     silent widening; no missing declared key = no stale declaration).
  B. `find_unguarded_widened_keys` — an `ast` tripwire: every widened key must
     appear in a presence guard (`"k" in p`, `"k" not in p`, `p.get("k"...)`, or
     a literal key handed to a helper alongside the payload) somewhere in the
     package's apply module source.

Fold a widened key into `payload_keys` only after one train has shipped with
the key emitted.
"""

from __future__ import annotations

import ast
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field

DECISION = "54447516"

RULE = (
    f"Decision {DECISION} (N-1 train compatibility): a payload key is added "
    "ADDITIVELY, declared in WIDENED_KEYS, and gated on presence "
    '(`if "<key>" in p`) in the apply module for one train; only after that '
    "train ships may it be folded into PAYLOAD_KEYS."
)


def _freeze(m: Mapping[str, Iterable[str]]) -> dict[str, frozenset[str]]:
    return {k: frozenset(v) for k, v in m.items()}


@dataclass(frozen=True)
class KeyInventory:
    """Per-event-type payload-key declaration.

    `payload_keys[t]`: the baseline — keys every N-1 peer already sends.
    `widened_keys[t]`: keys added since the baseline, applied only on presence.
    Both maps must cover the same event types (an empty set when nothing is
    widened) and be disjoint per type."""

    payload_keys: Mapping[str, frozenset[str]]
    widened_keys: Mapping[str, frozenset[str]] = field(default_factory=dict)

    def __post_init__(self) -> None:
        pk = _freeze(self.payload_keys)
        wk = _freeze(self.widened_keys)
        for t in pk:
            wk.setdefault(t, frozenset())
        if set(pk) != set(wk):
            raise ValueError(
                "KeyInventory: widened_keys declares event types absent from "
                f"payload_keys: {sorted(set(wk) - set(pk))}. {RULE}"
            )
        for t in pk:
            both = pk[t] & wk[t]
            if both:
                raise ValueError(
                    f"KeyInventory: {t} declares {sorted(both)} in BOTH payload_keys "
                    f"and widened_keys. {RULE}"
                )
        object.__setattr__(self, "payload_keys", pk)
        object.__setattr__(self, "widened_keys", wk)

    @property
    def event_types(self) -> frozenset[str]:
        return frozenset(self.payload_keys)

    def declared(self, event_type: str) -> frozenset[str]:
        return self.payload_keys[event_type] | self.widened_keys[event_type]

    def all_widened(self) -> frozenset[str]:
        out: frozenset[str] = frozenset()
        for v in self.widened_keys.values():
            out |= v
        return out


def check_emitted_keys(
    inventory: KeyInventory, event_type: str, payload: Mapping[str, object]
) -> list[str]:
    """Violations for ONE emitted payload (empty list = ok): undeclared keys
    (a silent widening) and declared keys the payload lacks (a stale
    declaration)."""
    if event_type not in inventory.payload_keys:
        return [
            (
                f"{event_type} is emitted but has no declaration in the "
                f"KeyInventory (PAYLOAD_KEYS / WIDENED_KEYS). {RULE}"
            )
        ]
    declared = inventory.declared(event_type)
    emitted = set(payload)
    out: list[str] = []
    for key in sorted(emitted - declared):
        out.append(
            f"`{key}` on {event_type} is emitted but not declared - a register "
            "WIDENING. Add it to WIDENED_KEYS with an apply-side presence guard "
            f'(`if "{key}" in p`), held for one train. {RULE}'
        )
    for key in sorted(declared - emitted):
        out.append(
            f"`{key}` on {event_type} is declared but this payload does not "
            "carry it - a stale declaration (the emitter no longer sends it, or "
            "sends it conditionally). A REMOVED key is also a skew: keep it "
            "optional on apply for one train before the declaration drops it. "
            f"{RULE}"
        )
    return out


def check_emitted_payloads(
    inventory: KeyInventory,
    payloads_by_type: Mapping[str, Iterable[Mapping[str, object]]],
    *,
    require_all_types: bool = True,
) -> list[str]:
    """`check_emitted_keys` over a whole driven run, plus (by default) a
    violation for every declared event type the run never emitted - a
    declaration nothing exercises guards nothing."""
    out: list[str] = []
    for event_type, payloads in sorted(payloads_by_type.items()):
        for payload in payloads:
            out.extend(check_emitted_keys(inventory, event_type, payload))
    if require_all_types:
        for event_type in sorted(inventory.event_types):
            if not list(payloads_by_type.get(event_type, ())):
                out.append(
                    f"{event_type} is declared but the full write surface never "
                    "emitted it - extend the emit test to exercise it (a "
                    "declaration nothing exercises guards nothing) or drop the "
                    "declaration."
                )
    return out


def _guarded_keys(tree: ast.AST, payload_names: frozenset[str]) -> set[str]:
    keys: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Compare):
            if (
                isinstance(node.left, ast.Constant)
                and isinstance(node.left.value, str)
                and any(isinstance(op, (ast.In, ast.NotIn)) for op in node.ops)
            ):
                keys.add(node.left.value)
        elif isinstance(node, ast.Call):
            first = node.args[0] if node.args else None
            is_str = isinstance(first, ast.Constant) and isinstance(first.value, str)
            if (
                is_str
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "get"
            ):
                keys.add(first.value)  # type: ignore[union-attr]
            else:
                # A literal key handed to a helper next to the payload itself,
                # e.g. `_register_stamp(payload, "lifecycle_authored_at")`.
                consts = [
                    a.value
                    for a in node.args
                    if isinstance(a, ast.Constant) and isinstance(a.value, str)
                ]
                if consts and any(
                    isinstance(a, ast.Name) and a.id in payload_names
                    for a in node.args
                ):
                    keys.update(consts)
    return keys


def find_unguarded_widened_keys(
    inventory: KeyInventory,
    apply_module_source: str,
    *,
    payload_names: Iterable[str] = ("p", "payload"),
) -> list[str]:
    """Violations (empty = ok) for every widened key with no presence guard in
    `apply_module_source` (the text of the apply module(s); concatenate several
    sources when apply spans modules). A tripwire for the guard's EXISTENCE, not
    proof of its semantics - a `.get("k")` that is then assigned unconditionally
    still satisfies it; review the guard shape when adding a widening."""
    guarded = _guarded_keys(ast.parse(apply_module_source), frozenset(payload_names))
    out: list[str] = []
    for event_type in sorted(inventory.widened_keys):
        for key in sorted(inventory.widened_keys[event_type]):
            if key not in guarded:
                out.append(
                    f"widened key `{key}` ({event_type}) has no presence guard in "
                    f'the apply module - expected `"{key}" in p` / `"{key}" not in '
                    f'p` / `p.get("{key}", ...)`. Without it an in-flight old-shape '
                    "event from an N-1 emitter nulls the replica's field on an LWW "
                    f"win. {RULE}"
                )
    return out
