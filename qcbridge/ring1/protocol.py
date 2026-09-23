"""Wire protocol: message formats, hot-state packing, blob chunking.

Everything here is bytes/dict in, bytes/dict out — no sockets, no bpy.
Channel semantics (decision #16, sync-protocol.md §Wire channels):
  hot   — one packed single-frame message, last-value (CONFLATE-safe).
  cold  — [header-json, payload] pairs, seq-numbered; gaps detected, never
          replayed.
  control — json dicts over DEALER/ROUTER with request-ids.
"""

from __future__ import annotations

import hmac
import json
import struct
from dataclasses import dataclass, field

PROTOCOL_VERSION = 2  # v2: camera-view mode + zoom/offset ride the hot state

# ── hot channel ──────────────────────────────────────────────────────────────

_HOT = struct.Struct("<4sBi3f16f3f")
_HOT_MAGIC = b"QCB2"
FLAG_PERSP = 1 << 0
FLAG_HOLD = 1 << 1  # reserved: follow/hold ships stage 3
FLAG_CAMERA = 1 << 2  # host viewport is looking through the camera
# Parity-spike probe (ring1/probe.py): optional trailing host wall clock +
# seq. Absent unless QCB_PROBE=1, so production packets are unchanged.
_HOT_PROBE = struct.Struct("<dI")


@dataclass(frozen=True)
class HotState:
    """Camera view + frame — the whole hot channel."""

    frame: int
    view_matrix: tuple  # 16 floats, row-major
    lens: float
    clip_start: float
    clip_end: float
    is_persp: bool = True
    hold: bool = False
    camera: bool = False       # view_perspective == 'CAMERA'
    cam_zoom: float = 0.0      # rv3d.view_camera_zoom
    cam_offset: tuple = (0.0, 0.0)
    t_host: float = 0.0        # probe only: host time.time() at sampling
    probe_seq: int = 0         # probe only

    def pack(self) -> bytes:
        flags = (
            (FLAG_PERSP if self.is_persp else 0)
            | (FLAG_HOLD if self.hold else 0)
            | (FLAG_CAMERA if self.camera else 0)
        )
        return _HOT.pack(
            _HOT_MAGIC, flags, self.frame,
            self.lens, self.clip_start, self.clip_end, *self.view_matrix,
            self.cam_zoom, self.cam_offset[0], self.cam_offset[1],
        ) + (_HOT_PROBE.pack(self.t_host, self.probe_seq) if self.t_host else b"")


def hot_core(data: bytes) -> bytes:
    """The view/frame part of a hot packet — what dedup compares. Probe
    stamps change every tick and must not defeat the static-view dedup."""
    return data[: _HOT.size]


def unpack_hot(data: bytes) -> HotState | None:
    t_host, probe_seq = 0.0, 0
    if len(data) == _HOT.size + _HOT_PROBE.size:
        t_host, probe_seq = _HOT_PROBE.unpack(data[_HOT.size:])
    elif len(data) != _HOT.size:
        return None
    (magic, flags, frame, lens, clip_start, clip_end,
     *rest) = _HOT.unpack(data[: _HOT.size])
    if magic != _HOT_MAGIC:
        return None
    matrix, cam = rest[:16], rest[16:]
    return HotState(
        frame=frame, view_matrix=tuple(matrix), lens=lens,
        clip_start=clip_start, clip_end=clip_end,
        is_persp=bool(flags & FLAG_PERSP), hold=bool(flags & FLAG_HOLD),
        camera=bool(flags & FLAG_CAMERA),
        cam_zoom=cam[0], cam_offset=(cam[1], cam[2]),
        t_host=t_host, probe_seq=probe_seq,
    )


# ── control channel ──────────────────────────────────────────────────────────

def encode_control(msg: dict) -> bytes:
    return json.dumps(msg, separators=(",", ":")).encode("utf-8")


def decode_control(data: bytes) -> dict | None:
    try:
        msg = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    return msg if isinstance(msg, dict) else None


def srt_passphrase(token: str) -> str:
    """The stream passphrase, derived from the session token on BOTH ends —
    one secret to configure, and the passphrase never crosses the wire
    (decision #16 amendment, 2026-07-26). Derivation is mandatory, not
    cosmetic: SRT requires 10–79 chars and tokens can be shorter. Empty
    token = unencrypted stream (VPN still encrypts the link)."""
    if not token:
        return ""
    import hashlib

    return hashlib.sha256(f"qcb-srt:{token}".encode()).hexdigest()[:32]


def make_hello(
    token: str, epoch: str, blender_version: str, addon_version: str = "",
    seq: int = 0, seq_fast: int = 0,
) -> dict:
    return {
        "kind": "hello",
        "token": token,
        "epoch": epoch,
        # Where the host's lane counters stand, so the replica primes its
        # trackers: a lost first message after a re-handshake is still a gap.
        "seq": seq,
        "seq_fast": seq_fast,
        "protocol": PROTOCOL_VERSION,
        "blender": blender_version,
        # Informational, never gates the handshake: mismatched ends mostly
        # WORK but degrade confusingly (e.g. unknown tier-1 path shapes
        # surface as apply errors) — both panels warn instead (2026-08-02).
        "addon": addon_version,
    }


def check_hello(msg: dict, expected_token: str) -> tuple[bool, str]:
    """Replica-side validation. Returns (ok, deny-reason)."""
    if msg.get("kind") != "hello":
        return False, "not a hello"
    if not hmac.compare_digest(str(msg.get("token", "")), expected_token):
        return False, "token mismatch"
    if msg.get("protocol") != PROTOCOL_VERSION:
        return False, f"protocol {msg.get('protocol')} != {PROTOCOL_VERSION}"
    return True, ""


# ── cold channel ─────────────────────────────────────────────────────────────
# Message kinds: "t1" (property deltas), "t2" (datablock blob), "tomb"
# (tombstone), "boot" (bootstrap blob). Fast-lane messages ("t1", "tomb")
# carry lane="f" and `after` (ring1/merge.py).

DEFAULT_CHUNK_SIZE = 4 * 1024 * 1024


def encode_cold(header: dict, payload: bytes = b"") -> list[bytes]:
    return [json.dumps(header, separators=(",", ":")).encode("utf-8"), payload]


def decode_cold(frames: list[bytes]) -> tuple[dict, bytes] | None:
    if len(frames) != 2:
        return None
    try:
        header = json.loads(frames[0].decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    return (header, frames[1]) if isinstance(header, dict) else None


def chunk_blob(
    kind: str,
    blob_id: str,
    data: bytes,
    meta: dict | None = None,
    chunk_size: int = DEFAULT_CHUNK_SIZE,
):
    """Yield (header, payload) cold messages for one blob. `meta` rides on
    every chunk (chunks may be inspected before the blob completes)."""
    total = max(1, -(-len(data) // chunk_size))
    view = memoryview(data)  # slices are views: no copy per chunk
    for i in range(total):
        header = {
            "kind": kind,
            "blob": {"id": blob_id, "i": i, "n": total, "size": len(data),
                     "o": i * chunk_size},  # offset: assemble in place
            **(meta or {}),
        }
        yield header, view[i * chunk_size : (i + 1) * chunk_size]


@dataclass
class _PartialBlob:
    total: int
    size: int
    header: dict
    buf: bytearray = field(default_factory=bytearray)  # the blob, assembled in place
    got: set = field(default_factory=set)


class Reassembler:
    """Collects chunks; returns the completed (header, bytes) exactly once.

    Runs on the receive thread — an apply item exists only when complete
    (sync-protocol.md: the apply loop never waits on the network). A fresh
    blob_id for the same datablock supersedes an incomplete older one at the
    caller's level; this class just tracks ids independently.
    """

    def __init__(self) -> None:
        self._partial: dict[str, _PartialBlob] = {}

    def feed(self, header: dict, payload: bytes) -> tuple[dict, bytes] | None:
        blob = header.get("blob")
        if not blob:
            return header, payload  # unchunked message passes straight through
        blob_id, i, n = blob["id"], blob["i"], blob["n"]
        part = self._partial.get(blob_id)
        if part is None:
            part = self._partial[blob_id] = _PartialBlob(
                total=n, size=blob["size"], header=header, buf=bytearray(blob["size"])
            )
        offset = blob.get("o", i * len(payload))
        end = offset + len(payload)
        if end > part.size:
            del self._partial[blob_id]
            return None  # corrupt chunk — drop; caller's gap detection escalates
        part.buf[offset:end] = payload  # the one copy, straight into place
        part.got.add(i)
        if len(part.got) < part.total:
            return None
        del self._partial[blob_id]
        return part.header, part.buf

    def drop(self, blob_id: str) -> None:
        self._partial.pop(blob_id, None)

    def pending(self) -> int:
        return len(self._partial)


class SeqTracker:
    """Cold-channel sequence bookkeeping: detects gaps, never replays them
    (decision #16 — a detected gap means escalate, e.g. re-mark dirty or
    force bootstrap)."""

    def __init__(self) -> None:
        self.last_seen: int | None = None
        self.gaps = 0

    def observe(self, seq: int) -> bool:
        """Returns True if `seq` follows contiguously."""
        contiguous = self.last_seen is None or seq == self.last_seen + 1
        if not contiguous:
            self.gaps += 1
        if self.last_seen is None or seq > self.last_seen:
            self.last_seen = seq
        return contiguous
