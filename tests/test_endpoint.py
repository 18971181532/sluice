"""Endpoints on a stopwatch.

The harness here is deliberately not the simulator: it is a heap of timers and
a single wire, so a test can say "deliver nothing for two seconds and see what
the sender does" without any network model in the way.
"""

from __future__ import annotations

import heapq
import unittest
from typing import Callable, List, Optional

from sluice import endpoint
from sluice import packet as pk
from sluice.cc import Reno


class _Timer:
    def __init__(self, entry):
        self.entry = entry

    def cancel(self) -> None:
        self.entry[3] = False


class Clock:
    def __init__(self) -> None:
        self.time = 0.0
        self._heap: List[list] = []
        self._order = 0
        self.outbox: List[pk.Segment] = []

    def transmit(self, segment: pk.Segment) -> None:
        self.outbox.append(segment)

    @property
    def now(self) -> float:
        return self.time

    def after(self, delay: float, callback: Callable[[], None]) -> _Timer:
        entry = [self.time + max(delay, 0.0), self._order, callback, True]
        self._order += 1
        heapq.heappush(self._heap, entry)
        return _Timer(entry)

    def run_until(self, target: float) -> None:
        while self._heap and self._heap[0][0] <= target:
            when, _, callback, alive = heapq.heappop(self._heap)
            self.time = when
            if alive:
                callback()
        self.time = max(self.time, target)


class Wire:
    """Two endpoints and a delay.  Everything the endpoints do goes through it."""

    def __init__(self, one_way: float = 0.05) -> None:
        self.clock = Clock()
        self.one_way = one_way
        self.sender: Optional[endpoint.Sender] = None
        self.receiver: Optional[endpoint.Receiver] = None
        self.dropped = 0
        self.drop = lambda segment: False
        self.sent: List[pk.Segment] = []

    # -- Environment ----------------------------------------------------
    @property
    def now(self) -> float:
        return self.clock.now

    def after(self, delay: float, callback: Callable[[], None]) -> _Timer:
        return self.clock.after(delay, callback)

    def transmit(self, segment: pk.Segment) -> None:
        self.sent.append(segment)
        if self.drop(segment):
            self.dropped += 1
            return
        target = self.receiver if segment.src == self.sender.sport else self.sender
        assert target is not None
        self.clock.after(self.one_way, lambda: target.receive(segment))

    # -- convenience ----------------------------------------------------
    def run(self, seconds: float) -> None:
        self.clock.run_until(self.clock.now + seconds)

    def connect(self) -> None:
        self.sender.connect()
        self.run(4 * self.one_way)


def connected(one_way: float = 0.05, **kwargs) -> Wire:
    wire = Wire(one_way)
    wire.sender = endpoint.Sender(wire, 4000, 80, kwargs.pop("control", Reno()),
                                  **kwargs)
    wire.receiver = endpoint.Receiver(wire, 80, 4000)
    wire.connect()
    return wire


class HandshakeTests(unittest.TestCase):
    def test_three_legs_leave_both_sides_established(self):
        wire = Wire(0.05)
        wire.sender = endpoint.Sender(wire, 4000, 80, Reno())
        wire.receiver = endpoint.Receiver(wire, 80, 4000)
        wire.sender.connect()
        self.assertEqual(wire.sender.state, endpoint.SYN_SENT)
        wire.run(0.2)
        self.assertEqual(wire.sender.state, endpoint.ESTABLISHED)
        self.assertEqual(wire.receiver.state, endpoint.ESTABLISHED)
        self.assertEqual(wire.sender.snd_una, wire.sender.iss + 1)
        self.assertEqual(wire.receiver.rcv_nxt, wire.sender.iss + 1)

    def test_the_syn_is_retransmitted_until_answered(self):
        wire = Wire(2.0)
        wire.sender = endpoint.Sender(wire, 4000, 80, Reno())
        wire.receiver = endpoint.Receiver(wire, 80, 4000)
        wire.sender.connect()
        wire.run(1.1)
        self.assertEqual(wire.sender.stats.timeouts, 1)
        self.assertEqual(wire.sender.stats.retransmits, 1)
        self.assertAlmostEqual(wire.sender.rto, 2.0)

    def test_connect_twice_is_refused(self):
        wire = connected()
        with self.assertRaises(RuntimeError):
            wire.sender.connect()


class TransferTests(unittest.TestCase):
    def test_all_bytes_arrive_in_order(self):
        wire = connected()
        data = bytes(range(256)) * 20  # 5120 bytes
        wire.sender.write(data)
        wire.run(2.0)
        self.assertEqual(wire.receiver.read_all(), data)
        self.assertEqual(wire.sender.snd_una, wire.sender.data_end)
        self.assertEqual(wire.sender.stats.retransmits, 0)

    def test_the_window_opens_by_one_segment_per_ack_in_slow_start(self):
        wire = connected()
        wire.sender.write(b"x" * 4000)
        wire.run(0.2)
        self.assertGreater(wire.sender.cc.cwnd, 1000)
        self.assertEqual(wire.sender.state, endpoint.ESTABLISHED)

    def test_writing_before_the_handshake_queues_the_data(self):
        wire = Wire(0.05)
        wire.sender = endpoint.Sender(wire, 4000, 80, Reno())
        wire.receiver = endpoint.Receiver(wire, 80, 4000)
        wire.sender.connect()
        wire.sender.write(b"early")
        wire.run(0.5)
        self.assertEqual(wire.receiver.read_all(), b"early")

    def test_close_sends_a_fin_and_the_sender_finishes(self):
        wire = connected()
        wire.sender.write(b"bye")
        wire.run(0.5)
        wire.sender.close()
        wire.run(0.5)
        self.assertTrue(wire.sender.done)
        self.assertEqual(wire.receiver.state, endpoint.CLOSED)


class LossTests(unittest.TestCase):
    def test_a_lost_segment_is_retransmitted_after_the_rto(self):
        wire = connected()
        lost = {"done": False}

        def drop(segment):
            if segment.payload and not lost["done"]:
                lost["done"] = True
                return True
            return False

        wire.drop = drop
        wire.sender.write(b"y" * 3000)
        wire.run(0.3)
        before = wire.sender.stats.retransmits
        wire.run(1.5)
        self.assertGreater(wire.sender.stats.retransmits, before)
        self.assertEqual(wire.receiver.read_all(), b"y" * 3000)

    def test_three_duplicate_acks_retransmit_without_waiting_for_a_timer(self):
        wire = connected(control=Reno(initial_window=6))
        seen = {"data": 0}

        def drop(segment):
            if not segment.payload:
                return False
            seen["data"] += 1
            return seen["data"] == 2  # the second data segment of the connection

        wire.drop = drop
        wire.sender.write(b"z" * 8000)
        wire.run(1.0)
        self.assertGreaterEqual(wire.sender.stats.fast_retransmits, 1)
        self.assertEqual(wire.receiver.read_all(), b"z" * 8000)

    def test_karn_keeps_retransmissions_out_of_the_rtt_estimate(self):
        wire = connected()
        wire.drop = lambda segment: bool(segment.payload)
        wire.sender.write(b"q" * 1000)
        wire.run(1.2)
        self.assertEqual(wire.sender.stats.rtt_samples, 0,
                         "the only ACK covers a retransmission")

    def test_the_rto_doubles_each_time_and_is_capped(self):
        wire = connected()
        wire.drop = lambda segment: bool(segment.payload)
        wire.sender.write(b"w" * 1000)
        wire.run(1.1)
        first = wire.sender.rto
        wire.run(2.2)
        second = wire.sender.rto
        self.assertAlmostEqual(second, first * 2)
        self.assertLessEqual(second, endpoint.MAX_RTO)

    def test_a_timeout_drops_the_window_to_one_segment(self):
        wire = connected(control=Reno(initial_window=4))
        wire.sender.write(b"e" * 8000)
        wire.run(0.001)
        wire.drop = lambda segment: bool(segment.payload)
        wire.run(2.5)
        self.assertEqual(wire.sender.cc.cwnd, 1000)


class RttTests(unittest.TestCase):
    def test_a_clean_round_trip_produces_a_sample(self):
        wire = connected()
        wire.sender.write(b"r" * 3000)
        wire.run(0.5)
        self.assertGreater(wire.sender.stats.rtt_samples, 0)
        self.assertGreater(wire.sender.srtt, 0.09)
        # 100 ms of propagation, plus the receiver's 40 ms delayed ACK while
        # the window is only one segment wide.  135 ms is the honest number.
        self.assertLess(wire.sender.srtt, 0.16)
        self.assertGreaterEqual(wire.sender.rto, endpoint.MIN_RTO)

    def test_the_first_sample_sets_rttvar_to_half(self):
        wire = connected()
        wire.sender.write(b"s" * 1000)
        wire.run(0.2)
        self.assertAlmostEqual(wire.sender.rttvar, wire.sender.srtt / 2, places=3)


class ReceiverTests(unittest.TestCase):
    def _receiver(self, rcvbuf=65_535):
        clock = Clock()
        receiver = endpoint.Receiver(clock, 80, 4000, rcvbuf=rcvbuf)
        receiver.receive(pk.Segment(4000, 80, 99, flags=pk.SYN, window=65535))
        return clock, receiver

    def test_out_of_order_data_is_buffered_not_delivered(self):
        clock, receiver = self._receiver()
        receiver.receive(pk.Segment(4000, 80, receiver.rcv_nxt + 1000,
                                    payload=b"B" * 1000))
        self.assertEqual(receiver.delivered, b"")
        self.assertEqual(receiver.out_of_order, 1)
        receiver.receive(pk.Segment(4000, 80, receiver.rcv_nxt, payload=b"A" * 1000))
        self.assertEqual(receiver.delivered, b"A" * 1000 + b"B" * 1000)

    def test_a_duplicate_is_acknowledged_but_delivered_once(self):
        clock, receiver = self._receiver()
        segment = pk.Segment(4000, 80, receiver.rcv_nxt, payload=b"x" * 500)
        receiver.receive(segment)
        receiver.receive(segment)
        self.assertEqual(receiver.delivered, b"x" * 500)
        self.assertGreaterEqual(receiver.acks_sent, 2)

    def test_a_partially_overlapping_segment_contributes_only_its_new_bytes(self):
        clock, receiver = self._receiver()
        receiver.receive(pk.Segment(4000, 80, receiver.rcv_nxt, payload=b"abcde"))
        receiver.receive(pk.Segment(4000, 80, receiver.rcv_nxt - 3, payload=b"cdefg"))
        self.assertEqual(bytes(receiver.delivered), b"abcdefg")

    def test_the_advertised_window_shrinks_as_data_piles_up(self):
        clock, receiver = self._receiver(rcvbuf=2000)
        receiver.receive(pk.Segment(4000, 80, receiver.rcv_nxt, payload=b"a" * 1000))
        self.assertEqual(receiver.window, 1000)
        receiver.receive(pk.Segment(4000, 80, receiver.rcv_nxt, payload=b"b" * 1000))
        self.assertEqual(receiver.window, 0)

    def test_reading_reopens_the_window(self):
        clock, receiver = self._receiver(rcvbuf=2000)
        receiver.receive(pk.Segment(4000, 80, receiver.rcv_nxt, payload=b"a" * 2000))
        self.assertEqual(receiver.window, 0)
        receiver.read(1500)
        self.assertEqual(receiver.window, 1500)

    def test_delayed_ack_waits_for_the_timer(self):
        clock, receiver = self._receiver()
        receiver.receive(pk.Segment(4000, 80, receiver.rcv_nxt, payload=b"a" * 100))
        self.assertEqual(receiver.acks_sent, 1, "the SYN-ACK")
        clock.run_until(0.03)
        self.assertEqual(receiver.acks_sent, 1)
        clock.run_until(0.05)
        self.assertEqual(receiver.acks_sent, 2)

    def test_two_segments_produce_an_immediate_ack(self):
        clock, receiver = self._receiver()
        receiver.receive(pk.Segment(4000, 80, receiver.rcv_nxt, payload=b"a" * 100))
        receiver.receive(pk.Segment(4000, 80, receiver.rcv_nxt, payload=b"b" * 100))
        self.assertEqual(receiver.acks_sent, 2)

    def test_a_fin_closes_the_receiver(self):
        clock, receiver = self._receiver()
        receiver.receive(pk.Segment(4000, 80, receiver.rcv_nxt, flags=pk.FIN | pk.ACK))
        self.assertEqual(receiver.state, endpoint.CLOSED)
        self.assertEqual(receiver.rcv_nxt, 101, "the FIN consumes one sequence number")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
