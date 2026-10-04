"""Identity/recents persistence: tolerant reads, atomic writes, capped lists."""

import json

from app import config


def test_missing_file_is_created_on_load(tmp_path):
    path = tmp_path / "nested" / "identity.json"
    cfg = config.load(path)

    assert path.exists()
    on_disk = json.loads(path.read_text())
    assert on_disk["peer_id"] == cfg.peer_id
    assert on_disk["nickname"] == config.DEFAULT_NICKNAME
    assert on_disk["recent"] == []


def test_save_and_load_keep_the_peer_id_stable(tmp_path):
    path = tmp_path / "identity.json"
    first = config.load(path)
    first.nickname = "lyg"
    first.recent = ["/tmp/a.txt"]
    config.save(first, path)

    second = config.load(path)
    assert second.peer_id == first.peer_id  # the id must survive a restart
    assert second.nickname == "lyg"
    assert second.recent == ["/tmp/a.txt"]


def test_corrupt_file_falls_back_to_a_fresh_identity(tmp_path):
    path = tmp_path / "identity.json"
    path.write_text("{not json")

    cfg = config.load(path)
    assert cfg.peer_id
    # ...and the broken file is replaced so the next start is clean
    assert json.loads(path.read_text())["peer_id"] == cfg.peer_id


def test_invalid_fields_are_repaired(tmp_path):
    path = tmp_path / "identity.json"
    path.write_text(json.dumps({"peer_id": "  ", "nickname": 42, "recent": "nope"}))

    cfg = config.load(path)
    assert cfg.peer_id and not cfg.peer_id.isspace()
    assert cfg.nickname == config.DEFAULT_NICKNAME
    assert cfg.recent == []


def test_recent_entries_drop_non_strings_and_are_capped(tmp_path):
    path = tmp_path / "identity.json"
    path.write_text(
        json.dumps(
            {
                "peer_id": "x",
                "nickname": "y",
                "recent": [None, 1, {"a": 2}, "/keep/me"]
                + [f"/tmp/file-{i}" for i in range(20)],
            }
        )
    )

    cfg = config.load(path)
    assert cfg.recent[0] == "/keep/me"  # junk dropped, order preserved
    assert len(cfg.recent) == config.RECENT_LIMIT == 12
    assert all(isinstance(p, str) for p in cfg.recent)


def test_push_recent_moves_picks_to_the_front(tmp_path):
    cfg = config.AppConfig(peer_id="x", nickname="y")
    cfg.recent = ["/old/1", "/old/2"]

    config.push_recent(cfg, ["/new/a", "/new/b"])
    assert cfg.recent[:2] == ["/new/a", "/new/b"]
    assert "/old/1" in cfg.recent

    # re-picking an older path moves it forward instead of duplicating it
    config.push_recent(cfg, ["/old/1"])
    assert cfg.recent.count("/old/1") == 1
    assert cfg.recent[0] == "/old/1"


def test_push_recent_caps_at_the_limit():
    cfg = config.AppConfig(peer_id="x", nickname="y")
    cfg.recent = [f"/tmp/{i}" for i in range(config.RECENT_LIMIT)]
    config.push_recent(cfg, ["/tmp/newest"])
    assert len(cfg.recent) == config.RECENT_LIMIT
    assert cfg.recent[0] == "/tmp/newest"


def test_default_path_honours_xdg_config_home(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    assert config.default_path() == tmp_path / "lanlink" / "identity.json"
    monkeypatch.delenv("XDG_CONFIG_HOME")
    assert config.default_path().name == "identity.json"
