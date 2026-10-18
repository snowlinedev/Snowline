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

A third declaration, `optional_keys`, covers CONDITIONAL wire keys (e.g. the
§7.1 backfill enumerator's per-register author stamps): allowed on a payload
but never required, so Test A checks them subset-only. They need an apply-side
presence guard like a widened key does.
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

    `payload_keys[t]`: the baseline - keys every N-1 peer already sends.
    `widened_keys[t]`: keys added since the baseline, applied only on presence.
    `optional_keys[t]`: CONDITIONAL keys (sent only on some paths, e.g. a
    backfill enumerator) - allowed, never required; applied only on presence.
    The maps must cover event types in `payload_keys` (an empty set when
    nothing is widened/optional) and be disjoint per type."""

    payload_keys: Mapping[str, frozenset[str]]
    widened_keys: Mapping[str, frozenset[str]] = field(default_factory=dict)
    optional_keys: Mapping[str, frozenset[str]] = field(default_factory=dict)

    def __post_init__(self) -> None:
        pk = _freeze(self.payload_keys)
        wk = _freeze(self.widened_keys)
        ok = _freeze(self.optional_keys)
        for t in pk:
            wk.setdefault(t, frozenset())
            ok.setdefault(t, frozenset())
        for name, m in (("widened_keys", wk), ("optional_keys", ok)):
            if set(m) != set(pk):
                raise ValueError(
                    f"KeyInventory: {name} declares event types absent from "
                    f"payload_keys: {sorted(set(m) - set(pk))}. {RULE}"
                )
        for t in pk:
            for a, b, label in (
                (pk[t], wk[t], "payload_keys and widened_keys"),
                (pk[t], ok[t], "payload_keys and optional_keys"),
                (wk[t], ok[t], "widened_keys and optional_keys"),
            ):
                both = a & b
                if both:
                    raise ValueError(
                        f"KeyInventory: {t} declares {sorted(both)} in BOTH "
                        f"{label}. {RULE}"
                    )
        object.__setattr__(self, "payload_keys", pk)
        object.__setattr__(self, "widened_keys", wk)
        object.__setattr__(self, "optional_keys", ok)

    @property
    def event_types(self) -> frozenset[str]:
        return frozenset(self.payload_keys)

    def declared(self, event_type: str) -> frozenset[str]:
        """The keys a payload MUST carry: baseline + widened."""
        return self.payload_keys[event_type] | self.widened_keys[event_type]

    def allowed(self, event_type: str) -> frozenset[str]:
        """The keys a payload MAY carry: declared + optional."""
        return self.declared(event_type) | self.optional_keys[event_type]

    def all_widened(self) -> frozenset[str]:
        out: frozenset[str] = frozenset()
        for v in self.widened_keys.values():
            out |= v
        return out

    def all_guarded(self) -> dict[str, str]:
        """key -> event type, for every key apply must read only on presence
        (widened and optional)."""
        out: dict[str, str] = {}
        for m in (self.widened_keys, self.optional_keys):
            for t in sorted(m):
                for k in sorted(m[t]):
                    out.setdefault(k, t)
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
    for key in sorted(emitted - inventory.allowed(event_type)):
        out.append(
            f"`{key}` on {event_type} is emitted but not declared - a register "
            "WIDENING. Add it to WIDENED_KEYS (or OPTIONAL_KEYS if conditional) "
            "with an apply-side presence guard "
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
    """Keys with a guard-shaped read on a PAYLOAD name: `"k" in p`,
    `"k" not in p`, `p.get("k"...)`, or `helper(p, "k")`. A `.get("k")` on any
    other receiver does not count - an unrelated dict elsewhere in the module
    must not satisfy the tripwire."""

    def is_payload(n: ast.AST) -> bool:
        return isinstance(n, ast.Name) and n.id in payload_names

    keys: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Compare):
            if (
                isinstance(node.left, ast.Constant)
                and isinstance(node.left.value, str)
                and any(
                    isinstance(op, (ast.In, ast.NotIn)) and is_payload(c)
                    for op, c in zip(node.ops, node.comparators, strict=True)
                )
            ):
                keys.add(node.left.value)
        elif isinstance(node, ast.Call):
            first = node.args[0] if node.args else None
            if (
                isinstance(first, ast.Constant)
                and isinstance(first.value, str)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "get"
            ):
                if is_payload(node.func.value):
                    keys.add(first.value)
            elif any(is_payload(a) for a in node.args):
                # A literal key handed to a helper next to the payload itself,
                # e.g. `_register_stamp(payload, "lifecycle_authored_at")`.
                keys.update(
                    a.value
                    for a in node.args
                    if isinstance(a, ast.Constant) and isinstance(a.value, str)
                )
    return keys


def find_unguarded_widened_keys(
    inventory: KeyInventory,
    apply_module_source: str,
    *,
    payload_names: Iterable[str] = ("p", "payload"),
) -> list[str]:
    """Violations (empty = ok) for every widened or optional key with no
    guard-shaped read of the payload in `apply_module_source` (the text of the
    apply module(s); concatenate several when apply spans modules). A tripwire
    that CHECKS a guard-shaped read exists, not that it is correct - a
    `p.get("k")` that is then assigned unconditionally still satisfies it;
    review the guard shape when adding a widening."""
    guarded = _guarded_keys(ast.parse(apply_module_source), frozenset(payload_names))
    out: list[str] = []
    for key, event_type in sorted(inventory.all_guarded().items()):
        if key not in guarded:
            out.append(
                f"widened/optional key `{key}` ({event_type}) has no presence "
                f'guard in the apply module - expected `"{key}" in p` / `"{key}" '
                f'not in p` / `p.get("{key}", ...)` on a payload name. Without '
                "it an in-flight old-shape event from an N-1 emitter nulls the "
                f"replica's field on an LWW win. {RULE}"
            )
    return out
