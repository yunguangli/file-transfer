"""Persistent identity and recents.

Stored as one small JSON file (default ``~/.config/lanlink/identity.json``)
so a restart keeps the same peer id, nickname and recent-file list. Every
read is defensive: a corrupt or hand-edited file must never stop the app
from starting.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional, Sequence

from .models import new_id

DEFAULT_NICKNAME = "unnamed"
RECENT_LIMIT = 12
CONFIG_DIR = "lanlink"
CONFIG_FILE = "identity.json"


def default_path() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / CONFIG_DIR / CONFIG_FILE


@dataclass
class AppConfig:
    peer_id: str = field(default_factory=new_id)
    nickname: str = DEFAULT_NICKNAME
    recent: list[str] = field(default_factory=list)


def load(path: Optional[Path] = None) -> AppConfig:
    """Read the config, creating it if missing or unreadable."""
    target = path or default_path()
    try:
        raw = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        cfg = AppConfig()
        save(cfg, target)
        return cfg
    if not isinstance(raw, dict):
        cfg = AppConfig()
        save(cfg, target)
        return cfg

    peer_id = raw.get("peer_id")
    if not isinstance(peer_id, str) or not peer_id.strip():
        peer_id = new_id()
    nickname = raw.get("nickname")
    if not isinstance(nickname, str) or not nickname.strip():
        nickname = DEFAULT_NICKNAME
    recent_raw = raw.get("recent")
    recent = [p for p in recent_raw if isinstance(p, str)][:RECENT_LIMIT] if isinstance(recent_raw, list) else []

    cfg = AppConfig(peer_id=peer_id.strip(), nickname=nickname, recent=recent)
    # Persist immediately: the peer id must survive even if the user never
    # touches the settings again.
    if not target.exists():
        save(cfg, target)
    return cfg


def save(cfg: AppConfig, path: Optional[Path] = None) -> Path:
    target = path or default_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(
        {"peer_id": cfg.peer_id, "nickname": cfg.nickname, "recent": cfg.recent},
        indent=2,
    )
    tmp = target.with_suffix(".tmp")
    tmp.write_text(payload, encoding="utf-8")
    tmp.replace(target)
    return target


def push_recent(cfg: AppConfig, paths: Sequence[str]) -> None:
    """Move picked paths to the front of the recents list, deduped and capped."""
    fresh = [p for p in paths if isinstance(p, str) and p]
    merged = fresh + [p for p in cfg.recent if p not in fresh]
    cfg.recent = merged[:RECENT_LIMIT]
