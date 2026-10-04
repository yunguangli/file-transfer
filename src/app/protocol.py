"""App-level wire protocol for a transfer.

lanlink gives us a framed JSON handshake plus a raw byte stream of exactly N
bytes; this module defines what goes *inside* that envelope::

    caller                                    answerer
      │── offer {v, type, sender, items, total} ─▶   lanlink validate()/accept()
      │◀─ ok  (or reject/busy, handled by lanlink) ──│
      │── payload: file bytes in manifest order ───▶ │  writes the same tree
      │◀─ {"reply":"ok","bytes":N} ──────────────────│

Keeping this separate from ``lanlink`` is deliberate: the transport stays
generic and stdlib-only, while file semantics (manifests, paths, labels)
live here where they can evolve — and be unit-tested — independently.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Sequence

from .models import Batch, Item

PROTOCOL_VERSION = 1
OFFER_TYPE = "offer"

# A human has to read a dialog and tap Accept; callers wait this long for it.
ANSWER_TIMEOUT = 90.0
MAX_ITEMS = 20_000
MAX_PATH_LEN = 400

_WINDOWS_DRIVE = re.compile(r"^[A-Za-z]:")


class ProtocolError(Exception):
    """The peer sent something malformed, inconsistent or unsafe."""


# --- building an offer ---------------------------------------------------


def normalize_relpath(raw: object) -> str:
    """Accept only safe, relative, forward-slash paths.

    Applied on both ends: when *we* pick files and again when we parse an
    offer from someone else, so a hostile peer cannot escape the destination
    folder with ``../../`` or an absolute path.
    """
    if not isinstance(raw, str) or not raw or len(raw) > MAX_PATH_LEN:
        raise ProtocolError(f"bad path: {raw!r}")
    unified = raw.replace("\\", "/").strip("/")
    if not unified or _WINDOWS_DRIVE.match(unified):
        raise ProtocolError(f"non-relative path: {raw!r}")
    parts = [p for p in unified.split("/")]
    if any(p in ("", ".", "..") for p in parts):
        raise ProtocolError(f"unsafe path: {raw!r}")
    return "/".join(parts)


def collect_items(paths: Sequence[str]) -> tuple[Item, ...]:
    """Turn picked filesystem paths into manifest items.

    Folders are expanded depth-first (sorted, so both sides agree on order)
    and keep their own name as the first path segment — the receiver
    rebuilds exactly what the sender selected. Empty folders are preserved
    as directory entries, and two picks that would collide (``a/photo.jpg``
    and ``b/photo.jpg``) are disambiguated up front so the manifest never
    writes over itself.
    """
    items: list[Item] = []
    roots: dict[str, int] = {}

    def unique_root(name: str) -> str:
        """Make a top-level manifest name unique within this batch."""
        if name not in roots:
            roots[name] = 1
            return name
        roots[name] += 1
        stem, dot, ext = name.rpartition(".")
        base = stem if dot else name
        suffix = f" ({roots[name]}){dot + ext if dot else ''}"
        candidate = f"{base}{suffix}"
        while candidate in roots:
            roots[name] += 1
            suffix = f" ({roots[name]}){dot + ext if dot else ''}"
            candidate = f"{base}{suffix}"
        roots[candidate] = 1
        return candidate

    for raw in paths:
        path = Path(raw).expanduser().resolve()
        if path.is_file():
            name = unique_root(normalize_relpath(path.name))
            items.append(Item(path=name, size=path.stat().st_size, source=str(path)))
            continue
        if not path.is_dir():
            raise ProtocolError(f"not a file or folder: {raw}")

        root_name = unique_root(normalize_relpath(path.name))
        items.append(Item(path=root_name, is_dir=True, source=str(path)))
        for root, dirs, files in os.walk(path, followlinks=False):
            dirs.sort()
            files.sort()
            relative = Path(root).relative_to(path.parent).parts[1:]
            prefix = "/".join((root_name, *relative)) if relative else root_name
            for name in dirs:
                full = Path(root) / name
                items.append(
                    Item(path=f"{prefix}/{name}", is_dir=True, source=str(full))
                )
            for name in files:
                full = Path(root) / name
                if not full.is_file():
                    continue  # broken symlink / socket / device node
                items.append(
                    Item(
                        path=f"{prefix}/{name}",
                        size=full.stat().st_size,
                        source=str(full),
                    )
                )

    if not items:
        raise ProtocolError("nothing to send")
    if len(items) > MAX_ITEMS:
        raise ProtocolError(f"too many entries: {len(items)} > {MAX_ITEMS}")
    return tuple(items)


def make_offer(
    batch: Batch,
    *,
    sender_id: str,
    sender_name: str,
    sender_ip: str = "",
    origin: dict[str, str] | None = None,
) -> dict:
    """Serialize a batch as the lanlink handshake dict.

    ``origin`` maps each top-level name to the absolute path it came from, so
    the receiver's history can show real names; it is advisory metadata and
    never used to build a path. ``sender_ip`` is display-only (lanlink tells
    us the peer's address only once the payload starts).
    """
    sender = {"id": sender_id, "name": sender_name}
    if sender_ip:
        sender["ip"] = sender_ip
    offer = {
        "v": PROTOCOL_VERSION,
        "type": OFFER_TYPE,
        "batch": batch.batch_id,
        "sender": sender,
        "label": batch.label,
        "items": [
            {"p": i.path, "s": i.size, "d": i.is_dir} for i in batch.items
        ],
        "total": batch.total_bytes,
    }
    if origin:
        offer["origin"] = dict(origin)
    return offer


# --- parsing an offer ----------------------------------------------------


@dataclass(frozen=True)
class Offer:
    """A validated incoming offer."""

    batch_id: str
    sender_id: str
    sender_name: str
    label: str
    items: tuple[Item, ...]
    total: int
    sender_ip: str = ""
    origin: tuple[str, ...] = ()

    @property
    def batch(self) -> Batch:
        """Same shape the sender had — handy for history and progress UI."""
        return Batch(items=self.items, batch_id=self.batch_id)

    @property
    def file_count(self) -> int:
        return sum(1 for i in self.items if not i.is_dir)


def parse_offer(header: object) -> Offer:
    """Validate an incoming handshake, raising ProtocolError when untrusted."""
    if not isinstance(header, dict):
        raise ProtocolError("offer must be a JSON object")
    if header.get("v") != PROTOCOL_VERSION:
        raise ProtocolError(f"unsupported protocol version: {header.get('v')!r}")
    if header.get("type") != OFFER_TYPE:
        raise ProtocolError(f"unexpected message type: {header.get('type')!r}")

    sender = header.get("sender")
    if not isinstance(sender, dict):
        raise ProtocolError("offer is missing sender")
    sender_id = sender.get("id")
    sender_name = sender.get("name")
    if not isinstance(sender_id, str) or not sender_id:
        raise ProtocolError("offer has no sender id")
    if not isinstance(sender_name, str):
        raise ProtocolError("offer has no sender name")
    sender_ip = sender.get("ip")
    if not isinstance(sender_ip, str):
        sender_ip = ""

    raw_items = header.get("items")
    if not isinstance(raw_items, list) or not raw_items:
        raise ProtocolError("offer has no items")
    if len(raw_items) > MAX_ITEMS:
        raise ProtocolError("offer has too many items")

    items: list[Item] = []
    payload_total = 0
    for raw in raw_items:
        if not isinstance(raw, dict):
            raise ProtocolError("malformed item")
        is_dir = bool(raw.get("d", False))
        size = raw.get("s", 0)
        if not isinstance(size, int) or isinstance(size, bool) or size < 0:
            raise ProtocolError(f"bad size for {raw.get('p')!r}")
        items.append(Item(path=normalize_relpath(raw.get("p")), size=size, is_dir=is_dir))
        if not is_dir:
            payload_total += size

    total = header.get("total")
    if not isinstance(total, int) or isinstance(total, bool) or total != payload_total:
        raise ProtocolError(
            f"declared total {total!r} does not match manifest ({payload_total})"
        )

    batch_id = header.get("batch")
    if not isinstance(batch_id, str) or not batch_id:
        batch_id = "unknown"

    label = header.get("label")
    if not isinstance(label, str) or not label.strip():
        label = Batch(items=tuple(items)).label

    origin_raw = header.get("origin")
    origin: tuple[str, ...] = ()
    if isinstance(origin_raw, list):
        origin = tuple(str(x) for x in origin_raw if isinstance(x, str) and x)[:64]

    return Offer(
        batch_id=batch_id,
        sender_id=sender_id,
        sender_name=sender_name,
        label=label,
        items=tuple(items),
        total=total,
        sender_ip=sender_ip,
        origin=origin,
    )


# --- writing received payloads ------------------------------------------


def safe_destination(dest_root: Path, rel_path: str) -> Path:
    """Join a manifest path onto the destination folder without escaping it."""
    rel = normalize_relpath(rel_path)
    target = (dest_root / rel).resolve()
    root = dest_root.resolve()
    if target != root and root not in target.parents:
        raise ProtocolError(f"path escapes destination: {rel_path!r}")
    return target


def new_destination(base: Path, peer_name: str, *, now: str) -> Path:
    """``<base>/<peer>_<timestamp>/``, uniquified if it already exists."""
    base.mkdir(parents=True, exist_ok=True)
    safe_peer = re.sub(r"[^A-Za-z0-9._-]+", "-", peer_name).strip("-") or "peer"
    stem = f"{safe_peer}_{now}"
    candidate = base / stem
    suffix = 1
    while candidate.exists():
        candidate = base / f"{stem}_{suffix}"
        suffix += 1
    candidate.mkdir(parents=True)
    return candidate


class ManifestWriter:
    """Streams payload bytes into the files named by a manifest, in order.

    The transport only guarantees "N bytes"; this turns that flat stream back
    into the tree the sender picked, creating folders as it goes.
    """

    def __init__(self, dest_root: Path, items: Iterable[Item]) -> None:
        self.dest_root = dest_root
        self._files = [i for i in items if not i.is_dir]
        self._dirs = [i for i in items if i.is_dir]
        self._index = 0
        self._handle = None
        self._remaining = 0
        self._written = 0
        for item in self._dirs:
            safe_destination(dest_root, item.path).mkdir(parents=True, exist_ok=True)

    @property
    def written(self) -> int:
        return self._written

    @property
    def current_path(self) -> str:
        if self._index < len(self._files):
            return self._files[self._index].path
        return ""

    def _prepare_next(self) -> None:
        """Open the next non-empty file, materialising empty ones as we pass.

        Manifests may contain zero-byte files anywhere (and may end with
        them), but the payload only carries bytes for non-empty entries —
        so empties are created at the moment the writer moves over them.
        """
        while self._handle is None and self._index < len(self._files):
            item = self._files[self._index]
            target = safe_destination(self.dest_root, item.path)
            target.parent.mkdir(parents=True, exist_ok=True)
            if item.size == 0:
                target.touch()
                self._index += 1
                continue
            self._handle = target.open("wb")
            self._remaining = item.size

    def write(self, chunk: bytes) -> int:
        """Consume the next bytes of the payload; returns bytes written."""
        view = memoryview(chunk)
        taken = 0
        while taken < len(view):
            self._prepare_next()
            if self._handle is None:
                raise ProtocolError("peer sent more bytes than the manifest declares")
            want = min(len(view) - taken, self._remaining)
            data = view[taken : taken + want]
            self._handle.write(data)
            taken += want
            self._remaining -= want
            self._written += want
            if self._remaining == 0:
                self._handle.close()
                self._handle = None
                self._index += 1
        return taken

    def close(self) -> None:
        if self._handle is not None:
            self._handle.close()
            self._handle = None

    def finish(self) -> None:
        """Close and verify every declared file actually got its bytes."""
        self.close()
        while self._index < len(self._files):
            item = self._files[self._index]
            if item.size > 0:
                raise ProtocolError(f"payload ended before {item.path!r}")
            target = safe_destination(self.dest_root, item.path)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.touch()
            self._index += 1
