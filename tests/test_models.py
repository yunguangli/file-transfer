"""Pure data-model behaviour: batches, progress maths, deterministic hashing."""

from app.models import (
    Batch,
    Direction,
    Item,
    Phase,
    TransferProgress,
    TransferRecord,
    new_id,
    stable_hash,
)


def make_batch(*paths: str) -> Batch:
    return Batch(items=tuple(Item(path=p, size=len(p)) for p in paths))


# --- identity -------------------------------------------------------------


def test_new_id_is_unique():
    assert new_id() != new_id()
    assert len(new_id()) == 32


def test_stable_hash_does_not_depend_on_process_state():
    """`hash()` is salted per run; crc32 must not be, or the radar reshuffles."""
    assert stable_hash("peer-abc") == stable_hash("peer-abc")
    assert stable_hash("peer-abc") != stable_hash("peer-def")


# --- batch ----------------------------------------------------------------


def test_batch_totals_count_files_only():
    batch = Batch(
        items=(
            Item(path="Photos", is_dir=True),
            Item(path="Photos/a.txt", size=10),
            Item(path="Photos/sub", is_dir=True),
            Item(path="Photos/sub/b.bin", size=32),
        )
    )
    assert batch.total_bytes == 42
    assert batch.file_count == 2
    assert batch.dir_count == 2


def test_batch_label_prefers_a_single_pick():
    assert make_batch("report.pdf").label == "report.pdf"
    assert make_batch("Photos/cat.jpg", "Photos/dog.jpg").label == "Photos"
    assert Batch(items=()).label == "Nothing selected"


def test_batch_label_counts_multiple_picks():
    assert make_batch("a.txt", "b.txt").label == "2 files"
    assert Batch(
        items=(Item(path="Photos", is_dir=True), Item(path="a.txt"))
    ).label == "2 items"


def test_top_level_keeps_pick_order_without_duplicates():
    batch = make_batch("Photos/a.jpg", "Photos/b.jpg", "clip.mp4")
    assert batch.top_level == ("Photos", "clip.mp4")


def test_item_display_name_is_the_last_segment():
    assert Item(path="Photos/2024/cat.jpg").display_name == "cat.jpg"
    assert Item(path="clip.mp4").display_name == "clip.mp4"


# --- progress -------------------------------------------------------------


def test_percent_is_clamped_and_zero_safe():
    assert TransferProgress.start(0).percent == 0.0
    half = TransferProgress(done=5, total=10, started_at=1.0, updated_at=2.0)
    assert half.percent == 0.5
    over = TransferProgress(done=99, total=10, started_at=1.0, updated_at=2.0)
    assert over.percent == 1.0


def test_rate_and_eta_need_a_few_samples():
    fresh = TransferProgress(done=10, total=100, started_at=0.0, updated_at=0.1)
    assert fresh.rate == 0.0
    assert fresh.eta is None

    steady = TransferProgress(done=50, total=100, started_at=0.0, updated_at=1.0)
    assert steady.rate == 50.0
    assert steady.eta == 1.0


def test_advanced_keeps_total_and_seed_time():
    seed = TransferProgress.start(100)
    moved = seed.advanced(40, current_path="sub/b.bin")
    assert (moved.done, moved.total, moved.started_at) == (40, 100, seed.started_at)
    assert moved.current_path == "sub/b.bin"
    # a later call without a path keeps the previous one
    assert moved.advanced(50).current_path == "sub/b.bin"


def test_elapsed_never_goes_negative():
    p = TransferProgress(done=1, total=2, started_at=10.0, updated_at=9.0)
    assert p.elapsed == 0.0


# --- phases & records -----------------------------------------------------


def test_only_live_transfer_phases_are_busy():
    assert Phase.RINGING.busy and Phase.INCOMING.busy and Phase.TRANSFERRING.busy
    assert not Phase.IDLE.busy
    assert not Phase.DONE.busy


def test_record_duration_and_copy():
    rec = TransferRecord(
        direction=Direction.OUT,
        peer_name="Maya's Laptop",
        peer_host="192.168.1.24",
        label="photos",
        item_count=3,
        total_bytes=10,
        started_at=100.0,
        finished_at=116.0,
    )
    assert rec.duration == 16.0
    assert rec.verb == "Sent"
    assert rec.headline() == "Transfer complete"

    failed = TransferRecord(
        direction=Direction.IN,
        peer_name="studio-pc",
        peer_host="192.168.1.31",
        label="clip.mp4",
        item_count=1,
        total_bytes=5,
        started_at=0.0,
        ok=False,
        error="connection lost",
    )
    assert failed.verb == "Received"
    assert failed.headline() == "Transfer failed"
