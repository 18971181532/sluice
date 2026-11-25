"""The wire format, bit by bit."""

from __future__ import annotations

import unittest

from sluice import packet as pk
from sluice import seq


def sample(**overrides) -> pk.Segment:
    fields = dict(src=4000, dst=80, seq=1000, ack=2000, flags=pk.ACK, window=4096,
                  payload=b"hello")
    fields.update(overrides)
    return pk.Segment(**fields)


class RoundTripTests(unittest.TestCase):
    def test_round_trip_preserves_every_field(self):
        for flags in (0, pk.ACK, pk.SYN, pk.SYN | pk.ACK, pk.FIN | pk.ACK,
                      pk.PSH | pk.ACK, pk.RST, 0x3F):
            segment = sample(flags=flags)
            again = pk.Segment.decode(segment.encode())
            self.assertEqual(again, segment, f"flags {flags:#04x}")

    def test_round_trip_with_empty_and_binary_payloads(self):
        for payload in (b"", b"\x00" * 10, bytes(range(256)) * 4):
            segment = sample(payload=payload)
            self.assertEqual(pk.Segment.decode(segment.encode()).payload, payload)

    def test_encoded_length(self):
        segment = sample(payload=b"x" * 100)
        self.assertEqual(len(segment.encode()), pk.HEADER + 100)

    def test_maximum_payload(self):
        segment = sample(payload=b"z" * pk.MAX_PAYLOAD)
        self.assertEqual(len(pk.Segment.decode(segment.encode()).payload),
                         pk.MAX_PAYLOAD)

    def test_payload_over_the_limit_is_refused(self):
        with self.assertRaises(pk.PacketError):
            sample(payload=b"z" * (pk.MAX_PAYLOAD + 1))


class ChecksumTests(unittest.TestCase):
    def test_known_value(self):
        # RFC 1071's worked example.
        data = bytes.fromhex("0001f203f4f5f6f7")
        self.assertEqual(pk.checksum(data), 0x220D)

    def test_odd_length_is_padded(self):
        self.assertEqual(pk.checksum(b"\x00\x01\x02"), pk.checksum(b"\x00\x01\x02\x00"))

    def test_every_single_bit_flip_is_caught(self):
        """160 header bits plus the payload: flip each one and decode must fail."""
        good = sample(payload=b"payload").encode()
        for byte in range(len(good)):
            for bit in range(8):
                broken = bytearray(good)
                broken[byte] ^= 1 << bit
                with self.assertRaises(pk.PacketError, msg=f"byte {byte} bit {bit}"):
                    pk.Segment.decode(bytes(broken))

    def test_checksum_field_itself_is_excluded(self):
        data = bytearray(sample().encode())
        data[16:18] = b"\xaa\xbb"
        with self.assertRaises(pk.PacketError):
            pk.Segment.decode(bytes(data))


class RejectTests(unittest.TestCase):
    def test_short_header(self):
        with self.assertRaises(pk.PacketError):
            pk.Segment.decode(b"\x00" * 19)

    def test_empty(self):
        with self.assertRaises(pk.PacketError):
            pk.Segment.decode(b"")

    def test_length_mismatch(self):
        data = sample(payload=b"abc").encode()
        with self.assertRaises(pk.PacketError):
            pk.Segment.decode(data[:-1])

    def test_reserved_byte_must_be_zero(self):
        data = bytearray(sample().encode())
        data[13] = 1
        data[16:18] = b"\x00\x00"
        data[16:18] = pk.checksum(bytes(data)).to_bytes(2, "big")
        with self.assertRaises(pk.PacketError):
            pk.Segment.decode(bytes(data))

    def test_port_out_of_range(self):
        with self.assertRaises(pk.PacketError):
            sample(src=70000)

    def test_negative_window(self):
        with self.assertRaises(pk.PacketError):
            sample(window=-1)

    def test_sequence_out_of_range(self):
        with self.assertRaises(pk.PacketError):
            sample(seq=seq.MOD)

    def test_flags_out_of_range(self):
        with self.assertRaises(pk.PacketError):
            sample(flags=256)


class DerivedTests(unittest.TestCase):
    def test_length_counts_data_and_control_bits(self):
        self.assertEqual(sample(payload=b"abc").length, 3)
        self.assertEqual(sample(flags=pk.SYN, payload=b"").length, 1)
        self.assertEqual(sample(flags=pk.FIN, payload=b"").length, 1)
        self.assertEqual(sample(flags=pk.SYN, payload=b"abc").length, 4)

    def test_end_wraps(self):
        segment = sample(seq=seq.MOD - 2, payload=b"abcd")
        self.assertEqual(segment.end, 2)

    def test_has(self):
        segment = sample(flags=pk.SYN | pk.ACK)
        self.assertTrue(segment.has(pk.SYN))
        self.assertTrue(segment.has(pk.ACK))
        self.assertFalse(segment.has(pk.FIN))


class TextTests(unittest.TestCase):
    def test_flag_names(self):
        self.assertEqual(pk.flags_to_text(pk.SYN | pk.ACK), "SYN,ACK")
        self.assertEqual(pk.flags_to_text(pk.ACK), "ACK")
        self.assertEqual(pk.flags_to_text(0), "-")
        self.assertEqual(pk.flags_to_text(0x80 | pk.ACK), "ACK,0x80")

    def test_describe_mentions_the_numbers(self):
        text = sample().describe()
        for piece in ("ACK", "1000", "2000", "4096", "5"):
            self.assertIn(piece, text)

    def test_str_is_readable(self):
        self.assertIn("4000->80", str(sample()))


class SegmentationTests(unittest.TestCase):
    def test_chunks_cover_the_stream_in_order(self):
        stream = bytes(range(256)) * 10
        pieces = list(pk.segments_for(stream, 1000, 1000))
        self.assertEqual(len(pieces), 3)
        self.assertEqual(b"".join(chunk for _, chunk in pieces), stream)
        self.assertEqual([s for s, _ in pieces], [1000, 2000, 3000])

    def test_last_chunk_is_short(self):
        pieces = list(pk.segments_for(b"abcde", 0, 2))
        self.assertEqual([len(c) for _, c in pieces], [2, 2, 1])

    def test_wraps_the_start(self):
        pieces = list(pk.segments_for(b"abcd", seq.MOD - 2, 2))
        self.assertEqual([s for s, _ in pieces], [seq.MOD - 2, 0])

    def test_empty_stream(self):
        self.assertEqual(list(pk.segments_for(b"", 5, 10)), [])

    def test_zero_mss(self):
        with self.assertRaises(ValueError):
            list(pk.segments_for(b"x", 0, 0))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
