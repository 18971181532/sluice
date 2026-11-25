"""Two endpoints, one connection.

The sender is where all the state lives: what has been sent, what has been
acknowledged, how long to wait before assuming the worst.  The receiver is
deliberately dull — it buffers out-of-order bytes, hands contiguous bytes to the
application, and advertises how much room is left.

The rules the sender follows are RFC 5681's, minus the parts that need options
(SACK, window scaling, timestamps).  Those are listed as non-goals in the
README; the parts that are here are here completely:

* the ACK clock — new data is sent only when an ACK opens the window
* RTT estimation per RFC 6298, with Karn's rule (never sample a retransmission)
* exponential backoff on timeout, capped at 60 seconds
* fast retransmit after three duplicate ACKs, with window inflation
* receiver flow control, including a zero window and a later window update

Everything is driven through a three-method environment (``now``, ``after``,
``transmit``) so a test can run a connection with no simulator at all.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Dict, List, Optional, Protocol

from . import packet as pk
from . import seq
from .cc import CongestionControl

# States.  CLOSING and TIME_WAIT are collapsed: this model has no half-closed
# connections and no need to wait out a segment's lifetime.
CLOSED = "CLOSED"
SYN_SENT = "SYN_SENT"
ESTABLISHED = "ESTABLISHED"
FIN_WAIT = "FIN_WAIT"

INITIAL_RTO = 1.0
MIN_RTO = 0.2
MAX_RTO = 60.0
CLOCK_GRANULARITY = 0.001
DELAYED_ACK = 0.04


class Timer(Protocol):
    def cancel(self) -> None: ...


class Environment(Protocol):
    """What an endpoint needs from the world."""

    @property
    def now(self) -> float: ...

    def after(self, delay: float, callback: Callable[[], None]) -> Timer: ...

    def transmit(self, segment: pk.Segment) -> None: ...


@dataclass
class Outstanding:
    segment: pk.Segment
    first_sent: float
    last_sent: float
    retransmits: int = 0


@dataclass
class Sample:
    time: float
    cwnd: int
    ssthresh: int
    event: str


@dataclass
class SenderStats:
    segments: int = 0
    retransmits: int = 0
    timeouts: int = 0
    fast_retransmits: int = 0
    duplicate_acks: int = 0
    bytes_acked: int = 0
    rtt_samples: int = 0
    first_ack: Optional[float] = None
    last_ack: Optional[float] = None


class Sender:
    """The half that keeps a clock and a conscience."""

    def __init__(
        self,
        env: Environment,
        sport: int,
        dport: int,
        control: CongestionControl,
        mss: int = 1000,
        iss: int = 1,
        rcvbuf: int = 65_535,
        delayed_ack: float = DELAYED_ACK,
    ) -> None:
        self.env = env
        self.sport = sport
        self.dport = dport
        self.cc = control
        self.mss = mss
        self.iss = iss
        self.state = CLOSED
        self.snd_una = iss
        self.snd_nxt = iss
        self.snd_wnd = rcvbuf
        self.rcv_nxt = 0
        self.rcv_window = rcvbuf

        self.unacked: Dict[int, Outstanding] = {}
        self.dupacks = 0
        self.in_recovery = False
        self.recover = 0
        self.stats = SenderStats()
        self.samples: List[Sample] = []
        self.rto_history: List[float] = []
        self._last_sample = -1.0

        # RFC 6298 state.
        self.srtt: Optional[float] = None
        self.rttvar = 0.0
        self.rto = INITIAL_RTO

        self._stream = bytearray()
        self._cursor = 0
        self._written = 0
        self._fin_sent = False
        self._fin_acked = False
        self._close_wanted = False
        self._timer: Optional[Timer] = None
        self._timer_generation = 0
        self._last_send = 0.0

    # -- application ----------------------------------------------------
    def connect(self) -> None:
        if self.state != CLOSED:
            raise RuntimeError("connect() called twice")
        self.state = SYN_SENT
        self.snd_nxt = seq.add(self.iss, 1)
        self._emit(self.iss, b"", pk.SYN)
        self._arm(self.rto)

    def write(self, data: bytes) -> None:
        if self.state not in (SYN_SENT, ESTABLISHED):
            raise RuntimeError(f"cannot write while {self.state}")
        self._stream.extend(data)
        self._written += len(data)
        self.pump()

    def close(self) -> None:
        if self._close_wanted:
            return
        self._close_wanted = True
        self.pump()

    @property
    def queued(self) -> int:
        """Bytes accepted from the application but not yet put on the wire."""
        return len(self._stream) - self._cursor

    @property
    def closing(self) -> bool:
        return self._close_wanted

    def compact(self) -> None:
        """Drop the already-segmented prefix so a long transfer stays small."""
        if self._cursor >= 1 << 16:
            del self._stream[: self._cursor]
            self._cursor = 0

    @property
    def data_end(self) -> int:
        """Sequence number just past the last byte the application supplied."""
        return seq.add(self.iss, 1 + self._written)

    @property
    def all_acked(self) -> bool:
        return self.state in (FIN_WAIT, CLOSED) and self.snd_una == self.data_end

    @property
    def done(self) -> bool:
        return self._fin_acked

    # -- sending --------------------------------------------------------
    def pump(self) -> None:
        """Send whatever the window allows, then possibly the FIN."""
        if self.state != ESTABLISHED:
            return
        while self._cursor < len(self._stream):
            allowed = min(self.cc.cwnd, self.snd_wnd)
            inflight = seq.distance(self.snd_nxt, self.snd_una)
            if inflight >= allowed:
                break
            chunk = bytes(self._stream[self._cursor : self._cursor + self.mss])
            if inflight + len(chunk) > allowed and inflight > 0:
                break  # a full segment does not fit; wait for the window
            self._emit(self.snd_nxt, chunk, pk.ACK | pk.PSH)
            self.snd_nxt = seq.add(self.snd_nxt, len(chunk))
            self._cursor += len(chunk)
        if (
            self._close_wanted
            and self._cursor >= len(self._stream)
            and not self._fin_sent
        ):
            self._emit(self.snd_nxt, b"", pk.FIN | pk.ACK)
            self.snd_nxt = seq.add(self.snd_nxt, 1)
            self._fin_sent = True
            self.state = FIN_WAIT
        self.compact()
        if self.unacked and self._timer is None:
            self._arm(self.rto)

    def _emit(self, at: int, payload: bytes, flags: int) -> None:
        segment = pk.Segment(
            src=self.sport,
            dst=self.dport,
            seq=at,
            ack=self.rcv_nxt,
            flags=flags,
            window=self.rcv_window,
            payload=payload,
        )
        self.unacked[at] = Outstanding(
            segment=segment, first_sent=self.env.now, last_sent=self.env.now
        )
        self.env.transmit(segment)
        self.stats.segments += 1

    def _retransmit_oldest(self) -> None:
        if not self.unacked:
            return
        at = next(iter(self.unacked))
        outstanding = self.unacked[at]
        again = pk.Segment(
            src=outstanding.segment.src,
            dst=outstanding.segment.dst,
            seq=outstanding.segment.seq,
            ack=self.rcv_nxt,
            flags=outstanding.segment.flags,
            window=self.rcv_window,
            payload=outstanding.segment.payload,
        )
        outstanding.last_sent = self.env.now
        outstanding.retransmits += 1
        self.stats.retransmits += 1
        self.env.transmit(again)

    # -- timers ---------------------------------------------------------
    def _arm(self, delay: float) -> None:
        self._timer_generation += 1
        generation = self._timer_generation
        if self._timer is not None:
            self._timer.cancel()
        self._timer = self.env.after(delay, lambda: self._on_timeout(generation))

    def _disarm(self) -> None:
        self._timer_generation += 1
        if self._timer is not None:
            self._timer.cancel()
            self._timer = None

    def _on_timeout(self, generation: int) -> None:
        if generation != self._timer_generation:
            return  # a stale timer that was cancelled too late
        self._timer = None
        if not self.unacked:
            return
        self.stats.timeouts += 1
        self.cc.on_loss(self.env.now, timeout=True)
        self.rto = min(self.rto * 2.0, MAX_RTO)
        self.rto_history.append(self.rto)
        self._record("timeout")
        self._retransmit_oldest()
        self._arm(self.rto)

    # -- receiving ------------------------------------------------------
    def receive(self, segment: pk.Segment) -> None:
        if segment.has(pk.RST):
            self.state = CLOSED
            self._disarm()
            return
        if segment.has(pk.SYN):
            self._handshake(segment)
            return
        if self.state in (CLOSED,):
            return
        self.snd_wnd = segment.window
        if segment.has(pk.ACK):
            self._on_ack(segment)
        if self.state == FIN_WAIT and self._fin_sent and not self.unacked:
            self._fin_acked = True
            self.state = CLOSED
            self._disarm()

    def _handshake(self, segment: pk.Segment) -> None:
        if not segment.has(pk.ACK):
            return
        self.snd_una = segment.ack
        self.snd_nxt = segment.ack
        self.unacked.pop(self.iss, None)
        self.rcv_nxt = seq.add(segment.seq, 1)
        self.rcv_window = segment.window
        self.snd_wnd = segment.window
        self.state = ESTABLISHED
        self._disarm()
        # Third leg of the handshake.
        self.env.transmit(
            pk.Segment(
                src=self.sport,
                dst=self.dport,
                seq=self.snd_nxt,
                ack=self.rcv_nxt,
                flags=pk.ACK,
                window=self.rcv_window,
            )
        )
        self._record("handshake")
        self.pump()

    def _on_ack(self, segment: pk.Segment) -> None:
        ack = segment.ack
        if seq.after(ack, self.snd_una):
            acked = seq.distance(ack, self.snd_una)
            sample: Optional[Outstanding] = None
            data = 0
            for at in list(self.unacked):
                outstanding = self.unacked[at]
                if seq.after(outstanding.segment.end, ack):
                    break  # this segment still has bytes beyond the ACK
                self.unacked.pop(at)
                data += len(outstanding.segment.payload)
                sample = outstanding
            self.snd_una = ack
            # ``acked`` covers sequence space, which includes SYN and FIN.
            # Throughput and the congestion window care about data only.
            self.stats.bytes_acked += data
            if self.stats.first_ack is None:
                self.stats.first_ack = self.env.now
            self.stats.last_ack = self.env.now
            self.dupacks = 0
            rtt = None
            if sample is not None and sample.retransmits == 0:
                rtt = self.env.now - sample.first_sent
                self._sample_rtt(rtt)
            self.cc.on_ack(data, self.env.now, rtt)
            if self.in_recovery:
                if seq.before(ack, self.recover):
                    # Partial ACK: the retransmission filled one hole and there
                    # is another one behind it.  Repair it now instead of
                    # waiting out an RTO.
                    self.cc.on_partial_ack(data)
                    self._record("partial-ack")
                    self._retransmit_oldest()
                else:
                    self.in_recovery = False
                    self.cc.deflate()
            self._record("ack")
            if self.unacked:
                self._arm(self.rto)
            else:
                self._disarm()
            self.pump()
            return

        # A duplicate ACK: same acknowledgement number, nothing new.
        if ack == self.snd_una and not segment.payload:
            self.stats.duplicate_acks += 1
            self.dupacks += 1
            if self.dupacks == 3 and not self.in_recovery and self.unacked:
                self.stats.fast_retransmits += 1
                self.recover = self.snd_nxt
                self.in_recovery = True
                self.cc.on_loss(self.env.now, timeout=False)
                self.cc.inflate(3 * self.mss)
                self._record("fast-retransmit")
                self._retransmit_oldest()
                self._arm(self.rto)
            elif self.dupacks > 3 and self.in_recovery:
                self.cc.inflate(self.mss)
                self._record("inflate")
                self.pump()

    def _sample_rtt(self, rtt: float) -> None:
        if rtt <= 0:
            return
        self.stats.rtt_samples += 1
        if self.srtt is None:
            self.srtt = rtt
            self.rttvar = rtt / 2.0
        else:
            self.rttvar = 0.75 * self.rttvar + 0.25 * abs(self.srtt - rtt)
            self.srtt = 0.875 * self.srtt + 0.125 * rtt
        self.rto = min(max(self.srtt + max(CLOCK_GRANULARITY, 4 * self.rttvar),
                           MIN_RTO), MAX_RTO)

    def _record(self, event: str) -> None:
        now = self.env.now
        if now - self._last_sample < 0.002 and event == "ack":
            return
        self._last_sample = now
        self.samples.append(Sample(now, self.cc.cwnd, self.cc.ssthresh, event))


class Receiver:
    """The half that mostly says "yes, and here is how much room is left"."""

    def __init__(
        self,
        env: Environment,
        sport: int,
        dport: int,
        iss: int = 5000,
        rcvbuf: int = 65_535,
        mss: int = 1000,
        delayed_ack: float = DELAYED_ACK,
    ) -> None:
        self.env = env
        self.sport = sport
        self.dport = dport
        self.iss = iss
        self.rcvbuf = rcvbuf
        self.mss = mss
        self.delayed_ack = delayed_ack
        self.state = CLOSED
        self.rcv_nxt = 0
        self.irs = 0
        self.read_seq = 0
        self.buffered: Dict[int, bytes] = {}
        self.delivered = bytearray()
        self.read_offset = 0
        self.acks_sent = 0
        self.out_of_order = 0
        self._pending_ack = 0
        self._ack_timer: Optional[Timer] = None

    # -- application ----------------------------------------------------
    def read(self, count: int = -1) -> bytes:
        """Hand bytes to the application and reopen the window if it was shut.

        A window update is sent only when the opening is worth a segment --
        otherwise every read would inject an ACK into the clock and the whole
        point of measuring the network would be lost.
        """
        before = self.window
        if count < 0:
            data = bytes(self.delivered[self.read_offset :])
        else:
            data = bytes(self.delivered[self.read_offset : self.read_offset + count])
        self.read_offset += len(data)
        self.read_seq = seq.add(self.read_seq, len(data))
        if self.state == ESTABLISHED and (
            self.window - before >= 2 * self.mss or (before == 0 and self.window > 0)
        ):
            self._send_ack()
        return data

    def read_all(self) -> bytes:
        return self.read(-1)

    def compact(self) -> int:
        """Forget bytes the application has already taken."""
        count = self.read_offset
        if count:
            del self.delivered[:count]
            self.read_offset = 0
        return count

    # -- window ---------------------------------------------------------
    @property
    def unread(self) -> int:
        return len(self.delivered) - self.read_offset

    @property
    def right_edge(self) -> int:
        """The highest sequence number the receiver will accept.

        This is the number that matters.  Advertising "free space left" instead
        would move the edge *left* whenever an out-of-order segment arrived --
        the sender would then discover that bytes it had already put on the
        wire were outside the window, and only a timeout could recover them.
        """
        return seq.add(self.read_seq, self.rcvbuf)

    @property
    def window(self) -> int:
        if self.state != ESTABLISHED:
            return self.rcvbuf
        return max(0, seq.distance(self.right_edge, self.rcv_nxt))

    # -- receiving ------------------------------------------------------
    def receive(self, segment: pk.Segment) -> None:
        if segment.has(pk.RST):
            self.state = CLOSED
            return
        if segment.has(pk.SYN):
            self.irs = segment.seq
            self.rcv_nxt = seq.add(segment.seq, 1)
            self.read_seq = self.rcv_nxt
            self.state = ESTABLISHED
            self._send_ack(flags=pk.SYN | pk.ACK, seq=self.iss)
            self.iss = seq.add(self.iss, 1)
            return
        if self.state != ESTABLISHED:
            return
        if segment.payload:
            self._on_data(segment)
        if segment.has(pk.FIN) and not self.buffered:
            if seq.compare(segment.seq, self.rcv_nxt) == 0:
                self.rcv_nxt = seq.add(self.rcv_nxt, 1)
            self._send_ack()
            self.state = CLOSED

    def _on_data(self, segment: pk.Segment) -> None:
        payload = segment.payload
        start = segment.seq

        relation = seq.compare(start, self.rcv_nxt)
        if relation < 0:
            overlap = seq.distance(self.rcv_nxt, start)
            if overlap >= len(payload):
                # A pure retransmission.  ACK it immediately so the sender's
                # duplicate-ACK counter moves.
                self._send_ack()
                return
            payload = payload[overlap:]
            start = self.rcv_nxt
            self._send_ack()
        elif relation > 0:
            offset = seq.distance(start, self.rcv_nxt)
            if offset + len(payload) <= self.window:
                self.buffered[start] = payload
                self.out_of_order += 1
            self._send_ack()
            return

        self.delivered.extend(payload)
        self.rcv_nxt = seq.add(self.rcv_nxt, len(payload))
        self._drain()

        self._pending_ack += 1
        if self.delayed_ack <= 0 or self._pending_ack >= 2:
            self._send_ack()
        elif self._ack_timer is None:
            self._ack_timer = self.env.after(self.delayed_ack, self._flush_ack)

    def _drain(self) -> None:
        while self.rcv_nxt in self.buffered:
            chunk = self.buffered.pop(self.rcv_nxt)
            self.delivered.extend(chunk)
            self.rcv_nxt = seq.add(self.rcv_nxt, len(chunk))

    def _flush_ack(self) -> None:
        self._ack_timer = None
        if self.state == ESTABLISHED:
            self._send_ack()

    def _send_ack(self, flags: int = pk.ACK, seq: Optional[int] = None) -> None:
        if self._ack_timer is not None:
            self._ack_timer.cancel()
            self._ack_timer = None
        self._pending_ack = 0
        self.acks_sent += 1
        self.env.transmit(
            pk.Segment(
                src=self.sport,
                dst=self.dport,
                seq=self.iss if seq is None else seq,
                ack=self.rcv_nxt,
                flags=flags,
                window=self.window,
            )
        )
