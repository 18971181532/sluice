#!/usr/bin/env python3
"""Drive a connection by hand, without the simulator.

The simulator in ``sluice.sim`` is convenient, but it is not required: an
endpoint only needs three things from the world -- the current time, a way to
schedule a callback, and a way to hand a segment to the other side.  This file
implements those three things in about forty lines, which is the smallest
honest proof that the layering is real.
"""

import heapq
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sluice import packet as pk  # noqa: E402
from sluice.cc import Reno  # noqa: E402
from sluice.endpoint import Receiver, Sender  # noqa: E402
from sluice.render import table  # noqa: E402

DELAY = 0.05  # one way, seconds
MESSAGE = (
    b"A sluice gate does not push the water. It decides how much may pass, "
    b"and the river does the rest."
)


class Timer:
    def __init__(self, entry):
        self.entry = entry

    def cancel(self):
        self.entry[3] = False


class Clock:
    """A heap of callbacks and a clock that only moves when it must."""

    def __init__(self):
        self.time = 0.0
        self._queue = []
        self._order = 0

    @property
    def now(self):
        return self.time

    def after(self, delay, callback):
        entry = [self.time + delay, self._order, callback, True]
        self._order += 1
        heapq.heappush(self._queue, entry)
        return Timer(entry)

    def run_until(self, target):
        while self._queue and self._queue[0][0] <= target:
            when, _, callback, alive = heapq.heappop(self._queue)
            self.time = when
            if alive:
                callback()
        self.time = max(self.time, target)


class Wire:
    """Two endpoints, one delay, and a log of everything that crossed."""

    def __init__(self, delay):
        self.clock = Clock()
        self.delay = delay
        self.sender = None
        self.receiver = None
        self.log = []

    # -- the three methods an endpoint needs ----------------------------
    @property
    def now(self):
        return self.clock.now

    def after(self, delay, callback):
        return self.clock.after(delay, callback)

    def transmit(self, segment):
        self.log.append((self.clock.now, segment))
        other = self.receiver if segment.src == self.sender.sport else self.sender
        self.clock.after(self.delay, lambda: other.receive(segment))


def main():
    wire = Wire(DELAY)
    wire.sender = Sender(wire, sport=4000, dport=80, control=Reno())
    wire.receiver = Receiver(wire, sport=80, dport=4000)

    wire.sender.connect()
    wire.sender.write(MESSAGE)
    wire.clock.run_until(2.0)

    received = wire.receiver.read_all()
    print(f"sent     {len(MESSAGE)} bytes")
    print(f"received {len(received)} bytes")
    print(f"identical: {received == MESSAGE}")
    print(f"segments on the wire: {len(wire.log)}")
    print()
    print("the first twelve segments, in the order they crossed:")
    print()
    rows = []
    for when, segment in wire.log[:12]:
        rows.append(
            [
                f"{when:.4f}",
                "->" if segment.src == 4000 else "<-",
                pk.flags_to_text(segment.flags),
                f"{segment.seq:,}",
                f"{segment.ack:,}",
                str(len(segment.payload)),
            ]
        )
    print(table(["time", "dir", "flags", "seq", "ack", "bytes"], rows,
                aligns=["right", "left", "left", "right", "right", "right"]))
    print()
    print("The handshake is three segments; after that every ACK opens the")
    print("window by one segment, which is why the window doubles per round trip.")


if __name__ == "__main__":
    main()
