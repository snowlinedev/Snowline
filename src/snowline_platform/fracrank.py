"""Decimal fractional indexing — the milestone release-line rank
(release-line.md §2.1, `line_rank`).

A COPY (behavior, not imports) of snowline-pm's `fracrank.py`, itself a port of
the frozen monolith's `snowline_server.fracrank`: the platform must never import
a plugin, and the module is pure, so the copy is verbatim in behavior.

Positions are `Decimal`; ascending order is line order. Inserting between two
members takes their midpoint, so no other row is rewritten — one row changes per
move, which is what lets the line replicate as plain per-row LWW. Decimal (not
float) math, so repeated bisection cannot drift two distinct positions into
collision. When the working precision cannot fit a strict midpoint any more,
`RebalanceNeeded` is raised and the caller renumbers its line 1..n and retries
(rare; a release line is short).
"""

from __future__ import annotations

from decimal import Decimal, localcontext

# Generous working precision for the midpoint division. ~40 significant digits
# allows >100 successive bisections between two adjacent integers before a
# rebalance is needed — far beyond a hand-maintained release line.
PRECISION = 40


class RebalanceNeeded(Exception):
    """No strict midpoint fits at the working precision; the caller renumbers
    its entries 1..n and retries."""


def between(low: Decimal | None, high: Decimal | None) -> Decimal:
    """A position strictly between `low` and `high`.

    - both None  -> the first position (1)
    - only high  -> a position before everything (high - 1)
    - only low   -> a position after everything (low + 1)
    - both       -> the decimal midpoint
    """
    if low is None and high is None:
        return Decimal(1)
    if low is None:
        return high - 1
    if high is None:
        return low + 1
    if low >= high:
        raise ValueError(f"low ({low}) must be strictly below high ({high})")
    with localcontext() as ctx:
        ctx.prec = PRECISION
        mid = (low + high) / Decimal(2)
    if mid <= low or mid >= high:
        raise RebalanceNeeded(
            f"no midpoint between {low} and {high} at precision {PRECISION}"
        )
    return mid


def to_wire(rank: Decimal | None) -> str | None:
    """A rank as its wire/JSON string — plain positional notation (never an
    exponent like `1E+1`), trailing zeros dropped, so equal ranks always
    serialize identically on every peer."""
    if rank is None:
        return None
    text = format(rank.normalize(), "f")
    return "0" if text in ("-0", "") else text


def from_wire(value) -> Decimal | None:
    """Parse a wire rank (string, or a number from a lenient client) back to a
    `Decimal`; None stays None. Raises `ValueError` on garbage."""
    if value is None:
        return None
    try:
        rank = Decimal(str(value))
    except Exception as exc:  # decimal.InvalidOperation
        raise ValueError(f"invalid line_rank {value!r}") from exc
    if not rank.is_finite():
        raise ValueError(f"invalid line_rank {value!r} — must be finite")
    return rank
