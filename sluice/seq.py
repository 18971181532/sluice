"""Sequence numbers live on a circle, not on a line.

TCP numbers every byte of a stream with a 32-bit counter that wraps.  Writing
``if a < b`` against that counter is wrong about once every four gigabytes, and
wrong *silently*: the connection stalls or, worse, accepts stale data.  Every
comparison in this project therefore goes through the five functions below,
which only ever look at a window smaller than 2**31 bytes.

The rule is the one from RFC 1982: a is behind b when the forward distance
from a to b is less than half the space.  Distances of exactly 2**31 are
not ordered; we break the tie by calling the larger-looking number "ahead",
which is arbitrary but deterministic.
"""

from __future__ import annotations

from typing import Tuple

MOD = 1 << 32
HALF = MOD >> 1
MASK = MOD - 1


def wrap(value: int) -> int:
    """Fold any integer into ``[0, 2**32)``."""
    return value & MASK


def add(seq: int, count: int) -> int:
    """Advance ``seq`` by ``count`` bytes, wrapping at 2**32."""
    return (seq + count) & MASK


def distance(a: int, b: int) -> int:
    """Signed forward distance from ``b`` to ``a``, in ``(-2**31, 2**31]``.

    ``distance(a, b) > 0`` means a is ahead of b.
    """
    gap = (a - b) & MASK
    return gap - MOD if gap > HALF else gap


def compare(a: int, b: int) -> int:
    """-1, 0 or 1, with the wraparound rule applied.

    ``a`` is behind ``b`` when the *forward* distance from ``a`` to ``b`` is
    less than half the space — that is RFC 1982's rule, and getting the
    direction of the subtraction backwards is exactly the bug this function
    exists to prevent.
    """
    gap = (b - a) & MASK
    if gap == 0:
        return 0
    return -1 if gap < HALF else 1


def before(a: int, b: int) -> bool:
    """True when ``a`` precedes ``b`` on the circle."""
    return compare(a, b) < 0


def after(a: int, b: int) -> bool:
    """True when ``a`` follows ``b`` on the circle."""
    return compare(a, b) > 0


def between(value: int, low: int, high: int) -> bool:
    """True when ``value`` lies in ``[low, high)``, wrapping if needed.

    ``low == high`` means the empty interval, which keeps this function honest
    for the degenerate case where a sender has nothing outstanding.
    """
    if low == high:
        return False
    if before(low, high):
        return not before(value, low) and before(value, high)
    # The interval crosses zero.
    return not before(value, low) or before(value, high)


def span(low: int, high: int) -> int:
    """How many bytes lie in ``[low, high)``; 0 when the interval is empty."""
    return (high - low) & MASK


def split(seq: int, length: int) -> Tuple[int, int]:
    """The two pieces of a segment that straddles the wrap point.

    Returns ``(head, tail)`` where ``head`` runs to 2**32 and ``tail`` starts at
    zero.  ``tail`` is 0 when the segment does not wrap.
    """
    if length < 0:
        raise ValueError("length must not be negative")
    room = MOD - seq
    if length <= room:
        return length, 0
    return room, length - room


def overlaps(a_start: int, a_len: int, b_start: int, b_len: int) -> int:
    """How many bytes two intervals share.  Used to trim retransmissions."""
    if a_len <= 0 or b_len <= 0:
        return 0
    # Move the origin to a_start so both intervals start at zero.
    b_off = distance(b_start, a_start)
    b_end = b_off + b_len
    if b_end <= 0 or b_off >= a_len:
        return 0
    return min(a_len, b_end) - max(0, b_off)
