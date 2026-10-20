from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Settings:
    photo_root: Path
    db_path: Path

    @classmethod
    def from_env(cls) -> "Settings":
        photo_root = Path(os.getenv("PHOTO_ROOT", "./sample-photos")).expanduser()
        db_path = Path(os.getenv("AURA_DB_PATH", "./data/aura.db")).expanduser()
        return cls(photo_root=photo_root, db_path=db_path)
