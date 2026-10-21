from __future__ import annotations

from fastapi import FastAPI
from pydantic import BaseModel, Field

from .config import Settings
from .config import ImmichConfig
from .db import Database
from .scanner import PhotoScanner
from .service import AuraService
from .events import build_day_candidates
from .query import parse_query
from .immich import ImmichClient, ImmichError, ImmichSettings

settings = Settings.from_env()
database = Database(settings.db_path)
service = AuraService(database)
immich_config = ImmichConfig.from_env()
app = FastAPI(title="Aura", version="0.1.0")


class ScanRequest(BaseModel):
    dry_run: bool = False


class SmartAlbumRequest(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    query: dict


class NamedRequest(BaseModel):
    name: str = Field(min_length=1, max_length=200)


class EventRequest(NamedRequest):
    level: str = Field(default="day", pattern="^(day|theme)$")
    confirmed: bool = False


class ExternalAssetRequest(BaseModel):
    provider: str = Field(default="immich", pattern="^[a-z0-9_-]+$")
    external_id: str = Field(min_length=1, max_length=200)


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok", "version": "0.1.0"}


@app.post("/api/v1/scans")
def scan(request: ScanRequest = ScanRequest()) -> dict[str, int]:
    return PhotoScanner(settings.photo_root, database).scan(dry_run=request.dry_run)


@app.get("/api/v1/assets")
def assets(q: str | None = None, start: str | None = None, end: str | None = None,
           person: str | None = None, tag: str | None = None, event: int | None = None) -> list[dict]:
    return service.search_assets(q, start, end, person, tag, event)


@app.get("/api/v1/people")
def people() -> list[dict]:
    return service.list_people()


@app.post("/api/v1/people", status_code=201)
def create_person(request: NamedRequest) -> dict:
    return service.create_person(request.name)


@app.get("/api/v1/tags")
def tags() -> list[dict]:
    return service.list_tags()


@app.post("/api/v1/tags", status_code=201)
def create_tag(request: NamedRequest) -> dict:
    return service.create_tag(request.name)


@app.get("/api/v1/events")
def events() -> list[dict]:
    return service.list_events()


@app.post("/api/v1/events/candidates")
def event_candidates() -> dict[str, int]:
    return {"created": build_day_candidates(database)}


@app.get("/api/v1/search/parse")
def parse_search(text: str) -> dict:
    return parse_query(text, [row["name"] for row in service.list_people()], [row["name"] for row in service.list_tags()])


@app.post("/api/v1/events", status_code=201)
def create_event(request: EventRequest) -> dict:
    return service.create_event(request.name, request.level, request.confirmed)


@app.post("/api/v1/smart-albums", status_code=201)
def smart_album(request: SmartAlbumRequest) -> dict:
    return service.create_smart_album(request.name, request.query)


@app.get("/api/v1/integrations/immich")
def immich_status() -> dict:
    if not immich_config.url or not immich_config.api_key:
        return {"configured": False, "reachable": False}
    try:
        info = ImmichClient(ImmichSettings(immich_config.url, immich_config.api_key)).server_info()
        return {"configured": True, "reachable": True, "server": info}
    except ImmichError as error:
        return {"configured": True, "reachable": False, "error": str(error)}


@app.post("/api/v1/integrations/immich/sync-assets")
def sync_immich_assets() -> dict[str, int]:
    if not immich_config.url or not immich_config.api_key:
        raise ValueError("IMMICH_URL and IMMICH_API_KEY must be configured")
    client = ImmichClient(ImmichSettings(immich_config.url, immich_config.api_key))
    return service.sync_immich_asset_mappings(client, immich_config.path_prefix)


@app.post("/api/v1/assets/{asset_id}/external", status_code=201)
def map_external_asset(asset_id: int, request: ExternalAssetRequest) -> dict:
    return service.map_external_asset(asset_id, request.provider, request.external_id)


@app.post("/api/v1/smart-albums/{smart_album_id}/materialize")
def materialize_smart_album(smart_album_id: int) -> dict:
    if not immich_config.url or not immich_config.api_key:
        raise ValueError("IMMICH_URL and IMMICH_API_KEY must be configured")
    return service.materialize_smart_album(smart_album_id, ImmichClient(ImmichSettings(immich_config.url, immich_config.api_key)))
