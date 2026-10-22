from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Settings:
    db_path: Path
    event_interval: int = 86400
    album_interval: int = 300

    @classmethod
    def from_env(cls) -> 'Settings':
        return cls(Path(os.getenv('AURA_DB_PATH', './data/aura.db')).expanduser(),
                   max(60, int(os.getenv('AURA_EVENT_INTERVAL', '86400'))),
                   max(30, int(os.getenv('AURA_ALBUM_INTERVAL', '300'))))


@dataclass(frozen=True)
class ImmichConfig:
    url: str
    api_key: str
    public_url: str = ''
    port: int = 2283
    job_api_key: str = ''

    @classmethod
    def from_env(cls) -> 'ImmichConfig':
        return cls(os.getenv('IMMICH_URL', '').rstrip('/'), os.getenv('IMMICH_API_KEY', ''),
                   os.getenv('IMMICH_PUBLIC_URL', '').rstrip('/'), int(os.getenv('IMMICH_PORT', '2283')), os.getenv('IMMICH_JOB_API_KEY', ''))
