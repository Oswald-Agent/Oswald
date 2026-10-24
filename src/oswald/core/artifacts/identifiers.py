"""Deterministic and time-ordered correlation identifiers for OSWALD tasks and artifacts."""

import os
import threading
import time
import uuid

_v7_lock = threading.Lock()
_last_v7_timestamp: int = 0
_last_v7_seq: int = 0


def uuid7() -> uuid.UUID:
    """Generate a UUIDv7 (RFC 9562) with monotonic sub-millisecond sequence ordering.

    Format:
        - 48 bits: unix timestamp in milliseconds
        - 4 bits: version 7 (0b0111)
        - 12 bits: monotonic sub-millisecond sequence counter
        - 2 bits: variant 2 (RFC 4122 / RFC 9562)
        - 62 bits: pseudo-random payload
    """
    global _last_v7_timestamp, _last_v7_seq

    with _v7_lock:
        ms = int(time.time() * 1000)
        if ms > _last_v7_timestamp:
            _last_v7_timestamp = ms
            _last_v7_seq = 0
        else:
            _last_v7_seq = (_last_v7_seq + 1) & 0x0FFF

        seq = _last_v7_seq
        timestamp = _last_v7_timestamp

    rand_bytes = os.urandom(8)
    time_high = (timestamp >> 16) & 0xFFFFFFFF
    time_mid = timestamp & 0xFFFF
    time_hi_version = 0x7000 | seq
    clock_seq_hi = (rand_bytes[0] & 0x3F) | 0x80
    clock_seq_low = rand_bytes[1]
    node = int.from_bytes(rand_bytes[2:], "big")

    return uuid.UUID(
        fields=(time_high, time_mid, time_hi_version, clock_seq_hi, clock_seq_low, node)
    )


def generate_task_id() -> str:
    """Generate a string UUIDv7 suitable for use as a task_id."""
    return str(uuid7())
