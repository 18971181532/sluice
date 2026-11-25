"""A twenty-byte TCP header, encoded by hand.

The real header is twenty bytes plus options; this one is twenty bytes and has
no options, no urgent pointer and no data offset (it is always five words).
Everything else is laid out where TCP puts it, because the point of the exercise
is that the wire format is not magic:

.. code-block:: text

     0                   1                   2                   3
     0 1 2 3 4 5 6 7 8 9 0 1 2 3 4 5 6 7 8 9 0 1 2 3 4 5 6 7 8 9 0 1
    +-------------------------------+-------------------------------+
    |          source port          |       destination port        |
    +-------------------------------+-------------------------------+
    |                        sequence number                        |
    +-------------------------------+-------------------------------+
    |                    acknowledgment number                      |
    +-------------------------------+-------------------------------+
    |  flags  |  reserved (must be 0) |            window             |
    +-------------------------------+-------------------------------+
    |           checksum            |         payload length        |
    +-------------------------------+-------------------------------+
    |                             payload                           |
    +---------------------------------------------------------------+

The checksum is the same one's-complement sum the real protocol uses, computed
over the header with the checksum field zeroed, followed by the payload.  It
catches bit rot; it does not stop a forger, and the docstring says so rather
than pretending otherwise.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass, field
from typing import Iterable, Tuple

from . import seq as seqmod

HEADER = 20
MAX_PAYLOAD = 65_495  # 65535 minus a header, rounded down to a sane bound

FIN = 0x01
SYN = 0x02
RST = 0x04
PSH = 0x08
ACK = 0x10

FLAG_NAMES: Tuple[Tuple[int, str], ...] = (
    (SYN, "SYN"),
    (ACK, "ACK"),
    (FIN, "FIN"),
    (RST, "RST"),
    (PSH, "PSH"),
)

_HEAD = struct.Struct(">HHIIBBHHH")  # network byte order, like the real thing


class PacketError(ValueError):
    """A byte string that cannot be a segment, or a segment that cannot exist."""


def flags_to_text(flags: int) -> str:
    """``0x12`` -> ``"SYN,ACK"``.  Unknown bits are shown in hex."""
    names = [name for bit, name in FLAG_NAMES if flags & bit]
    leftover = flags & ~sum(bit for bit, _ in FLAG_NAMES)
    if leftover:
        names.append(f"0x{leftover:02x}")
    return ",".join(names) if names else "-"


def checksum(data: bytes) -> int:
    """One's-complement 16-bit sum, the way RFC 1071 describes it."""
    if len(data) % 2:
        data += b"\x00"
    total = 0
    for index in range(0, len(data), 2):
        total += (data[index] << 8) | data[index + 1]
        total = (total & 0xFFFF) + (total >> 16)
    return (~total) & 0xFFFF


@dataclass(frozen=True)
class Segment:
    """One TCP segment.  Immutable so a trace can hold on to it safely."""

    src: int
    dst: int
    seq: int
    ack: int = 0
    flags: int = ACK
    window: int = 0
    payload: bytes = b""

    def __post_init__(self) -> None:
        for name, value, limit in (
            ("src", self.src, 0xFFFF),
            ("dst", self.dst, 0xFFFF),
            ("window", self.window, 0xFFFF),
        ):
            if not 0 <= value <= limit:
                raise PacketError(f"{name} {value} does not fit in 16 bits")
        for name, value in (("seq", self.seq), ("ack", self.ack)):
            if not 0 <= value < seqmod.MOD:
                raise PacketError(f"{name} {value} does not fit in 32 bits")
        if not 0 <= self.flags <= 0xFF:
            raise PacketError(f"flags {self.flags} does not fit in 8 bits")
        if len(self.payload) > MAX_PAYLOAD:
            raise PacketError(f"payload of {len(self.payload)} bytes is too large")

    # -- addressing -----------------------------------------------------
    @property
    def length(self) -> int:
        """Sequence space consumed: data, plus one for SYN and one for FIN."""
        extra = (1 if self.flags & SYN else 0) + (1 if self.flags & FIN else 0)
        return len(self.payload) + extra

    @property
    def end(self) -> int:
        """The sequence number just past this segment."""
        return seqmod.add(self.seq, self.length)

    def has(self, flag: int) -> bool:
        return bool(self.flags & flag)

    # -- wire format ----------------------------------------------------
    def encode(self) -> bytes:
        head = _HEAD.pack(
            self.src,
            self.dst,
            self.seq,
            self.ack,
            self.flags,
            0,  # reserved
            self.window,
            0,  # checksum placeholder
            len(self.payload),
        )
        body = head + self.payload
        return body[:16] + struct.pack(">H", checksum(body)) + body[18:]

    @classmethod
    def decode(cls, data: bytes) -> "Segment":
        if len(data) < HEADER:
            raise PacketError(f"{len(data)} bytes is shorter than a header")
        src, dst, seq, ack, flags, reserved, window, sum_, length = _HEAD.unpack(
            data[:HEADER]
        )
        if reserved != 0:
            raise PacketError(f"reserved byte is {reserved}, must be zero")
        if length != len(data) - HEADER:
            raise PacketError(
                f"header claims {length} payload bytes, got {len(data) - HEADER}"
            )
        expected = checksum(data[:16] + b"\x00\x00" + data[18:])
        if expected != sum_:
            raise PacketError(f"checksum {sum_:04x} does not match {expected:04x}")
        return cls(src, dst, seq, ack, flags, window, data[HEADER:])

    # -- presentation ---------------------------------------------------
    def describe(self) -> str:
        parts = [
            f"{flags_to_text(self.flags):<8}",
            f"seq {self.seq:>10}",
            f"ack {self.ack:>10}",
            f"win {self.window:>5}",
            f"len {len(self.payload):>4}",
        ]
        return "  ".join(parts)

    def __str__(self) -> str:  # pragma: no cover - convenience only
        return f"{self.src}->{self.dst} {self.describe()}"


def segments_for(stream: bytes, start_seq: int, mss: int) -> Iterable[Tuple[int, bytes]]:
    """Chop a byte string into ``(seq, chunk)`` pairs, wrapping as needed."""
    if mss <= 0:
        raise ValueError("mss must be positive")
    offset = 0
    while offset < len(stream):
        yield seqmod.add(start_seq, offset), stream[offset : offset + mss]
        offset += mss
