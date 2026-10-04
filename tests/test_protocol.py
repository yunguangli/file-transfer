"""App-level wire protocol: manifest safety, offer validation, payload replay."""

import pytest

from app import protocol
from app.models import Batch, Item
from app.protocol import (
    ManifestWriter,
    ProtocolError,
    collect_items,
    make_offer,
    new_destination,
    normalize_relpath,
    parse_offer,
    safe_destination,
)


# --- path hygiene ---------------------------------------------------------


@pytest.mark.parametrize(
    "raw",
    ["../../etc/passwd", "C:/Windows/System32", "a//b", "./a", "a/./b", ""],
)
def test_normalize_rejects_paths_that_escape(raw):
    with pytest.raises(ProtocolError):
        normalize_relpath(raw)


def test_normalize_relativizes_absolute_paths():
    """A leading slash is stripped, never honoured.

    ``/etc/passwd`` from a hostile peer must become ``etc/passwd`` *inside*
    the destination folder — that is what keeps ``safe_destination`` safe.
    """
    assert normalize_relpath("/etc/passwd") == "etc/passwd"


def test_normalize_accepts_ordinary_relative_paths():
    assert normalize_relpath("Photos/2024/cat.jpg") == "Photos/2024/cat.jpg"
    assert normalize_relpath("Photos/cat.jpg/") == "Photos/cat.jpg"


def test_safe_destination_refuses_to_leave_the_root(tmp_path):
    root = tmp_path / "incoming"
    root.mkdir()
    assert safe_destination(root, "Pack/a.txt") == (root / "Pack/a.txt").resolve()
    with pytest.raises(ProtocolError):
        safe_destination(root, "../outside.txt")


# --- picking --------------------------------------------------------------


def test_collect_items_records_the_absolute_source(tmp_path):
    (tmp_path / "a.txt").write_text("hello")
    (items,) = (collect_items([str(tmp_path / "a.txt")]),)
    assert items[0].path == "a.txt"
    assert items[0].size == 5
    assert items[0].source == str((tmp_path / "a.txt").resolve())


def test_collect_items_expands_a_folder_with_its_own_name(tmp_path):
    pack = tmp_path / "Pack"
    (pack / "sub").mkdir(parents=True)
    (pack / "a.txt").write_text("aaa")
    (pack / "sub" / "b.bin").write_bytes(b"b" * 10)

    items = collect_items([str(pack)])
    paths = [i.path for i in items]
    assert paths[0] == "Pack"
    assert paths == ["Pack", "Pack/sub", "Pack/a.txt", "Pack/sub/b.bin"]
    assert [i.is_dir for i in items] == [True, True, False, False]
    # every entry remembers where it came from, and dirs carry no size
    assert all(i.source for i in items)
    assert items[0].size == 0


def test_collect_items_disambiguates_colliding_names(tmp_path):
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    (tmp_path / "a" / "photo.jpg").write_text("1")
    (tmp_path / "b" / "photo.jpg").write_text("2")

    items = collect_items([str(tmp_path / "a" / "photo.jpg"), str(tmp_path / "b" / "photo.jpg")])
    assert [i.path for i in items] == ["photo.jpg", "photo (2).jpg"]

    folders = collect_items([str(tmp_path / "a"), str(tmp_path / "b")])
    assert [i.path for i in folders if i.is_dir] == ["a", "b"]


def test_collect_items_preserves_empty_folders(tmp_path):
    empty = tmp_path / "Empty"
    empty.mkdir()
    items = collect_items([str(empty)])
    assert [(i.path, i.is_dir, i.size) for i in items] == [("Empty", True, 0)]


def test_collect_items_rejects_a_missing_path(tmp_path):
    with pytest.raises(ProtocolError):
        collect_items([str(tmp_path / "nope.txt")])


# --- offer round trip -----------------------------------------------------


def test_offer_round_trip_and_ip_is_optional():
    batch = Batch(items=(Item(path="a.txt", size=5, source="/somewhere/a.txt"),))
    header = make_offer(
        batch, sender_id="aaaa", sender_name="Maya's Laptop", sender_ip="192.168.1.24"
    )
    offer = parse_offer(header)

    assert offer.sender_id == "aaaa"
    assert offer.sender_name == "Maya's Laptop"
    assert offer.sender_ip == "192.168.1.24"
    assert offer.total == 5
    assert offer.batch.label == "a.txt"
    assert offer.file_count == 1
    # the sender's absolute path never travels
    assert "source" not in header["items"][0]


def test_offer_without_ip_still_parses():
    header = make_offer(Batch(items=(Item(path="a.txt", size=1),)), sender_id="x", sender_name="y")
    assert "ip" not in header["sender"]
    assert parse_offer(header).sender_ip == ""


@pytest.mark.parametrize(
    "mutate, message",
    [
        (lambda h: h.update(v=99), "protocol version"),
        (lambda h: h.update(type="ping"), "message type"),
        (lambda h: h.pop("sender"), "sender"),
        (lambda h: h.update(items=[]), "items"),
        (lambda h: h.update(total=999), "manifest"),
        (lambda h: h["items"][0].update(p="../evil.txt"), "path"),
        (lambda h: h["items"][0].update(s=-4), "size"),
    ],
)
def test_parse_offer_rejects_malformed_headers(mutate, message):
    header = make_offer(Batch(items=(Item(path="a.txt", size=5),)), sender_id="x", sender_name="y")
    mutate(header)
    with pytest.raises(ProtocolError, match=message):
        parse_offer(header)


def test_parse_offer_needs_a_dict():
    with pytest.raises(ProtocolError):
        parse_offer(["not", "an", "object"])


# --- destination folder ---------------------------------------------------


def test_new_destination_is_peer_stamped_and_uniquified(tmp_path):
    first = new_destination(tmp_path / "in", "Maya's Laptop!", now="20261004-120000")
    assert first.parent == tmp_path / "in"
    assert first.name == "Maya-s-Laptop_20261004-120000"

    again = new_destination(tmp_path / "in", "Maya's Laptop!", now="20261004-120000")
    assert again != first
    assert again.name.endswith("_1")
    assert first.is_dir() and again.is_dir()


def test_new_destination_falls_back_to_peer_for_an_unusable_name(tmp_path):
    dest = new_destination(tmp_path, "///", now="t")
    assert dest.name == "peer_t"


# --- replaying the payload ------------------------------------------------


def build_manifest(tmp_path):
    items = [
        Item(path="Pack", is_dir=True),
        Item(path="Pack/a.txt", size=5),
        Item(path="Pack/empty", size=0),
        Item(path="Pack/sub", is_dir=True),
        Item(path="Pack/sub/b.bin", size=10),
    ]
    dest = tmp_path / "out"
    dest.mkdir()
    return dest, items


def test_manifest_writer_rebuilds_the_tree(tmp_path):
    dest, items = build_manifest(tmp_path)
    writer = ManifestWriter(dest, items)

    payload = b"hello" + b"x" * 10  # empty files contribute no bytes
    assert writer.write(payload) == len(payload)
    writer.finish()

    assert (dest / "Pack" / "a.txt").read_text() == "hello"
    assert (dest / "Pack" / "empty").read_bytes() == b""
    assert (dest / "Pack" / "sub" / "b.bin").read_bytes() == b"x" * 10
    assert (dest / "Pack" / "sub").is_dir()
    assert writer.written == len(payload)


def test_manifest_writer_tracks_the_file_it_is_on(tmp_path):
    dest, items = build_manifest(tmp_path)
    writer = ManifestWriter(dest, items)
    assert writer.current_path == "Pack/a.txt"
    writer.write(b"hello")
    # the zero-byte entry is reported until the writer steps over it
    assert writer.current_path == "Pack/empty"
    writer.write(b"x" * 10)
    assert writer.current_path == ""


def test_manifest_writer_rejects_a_short_payload(tmp_path):
    dest, items = build_manifest(tmp_path)
    writer = ManifestWriter(dest, items)
    writer.write(b"hello")
    with pytest.raises(ProtocolError, match="payload ended"):
        writer.finish()


def test_manifest_writer_rejects_extra_bytes(tmp_path):
    dest, items = build_manifest(tmp_path)
    writer = ManifestWriter(dest, items)
    with pytest.raises(ProtocolError, match="more bytes"):
        writer.write(b"hello" + b"x" * 10 + b"junk")
    writer.close()
