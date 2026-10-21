from __future__ import annotations

from io import BytesIO
from pathlib import Path
from typing import Literal

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from PIL import Image, ImageOps
from pydantic import BaseModel, Field, ConfigDict

from . import __version__
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
app = FastAPI(title="Aura", version=__version__)


class ScanRequest(BaseModel):
    dry_run: bool = False


class AlbumQuery(BaseModel):
    model_config = ConfigDict(extra="forbid")
    q: str | None = None
    person: str | None = None
    tag: str | None = None
    person_id: int | None = Field(default=None, gt=0)
    tag_id: int | None = Field(default=None, gt=0)
    event: int | None = Field(default=None, gt=0)
    start: str | None = None
    end: str | None = None


class SmartAlbumRequest(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    query: AlbumQuery


class ReviewRequest(BaseModel):
    state: Literal["pending", "confirmed", "rejected"]
    name: str | None = Field(default=None, min_length=1, max_length=200)


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
    return {"status": "ok", "version": __version__}


@app.post("/api/v1/scans")
def scan(request: ScanRequest = ScanRequest()) -> dict[str, int]:
    return PhotoScanner(settings.photo_root, database).scan(dry_run=request.dry_run)


@app.get("/api/v1/assets")
def assets(q: str | None = None, start: str | None = None, end: str | None = None,
           person: str | None = None, tag: str | None = None, event: int | None = None, person_id: int | None = None, tag_id: int | None = None) -> list[dict]:
    return service.search_assets(q, start, end, person, tag, event, person_id, tag_id)


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
    return service.create_smart_album(request.name, request.query.model_dump(exclude_none=True))


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
    return service.sync_immich_asset_mappings(client, immich_config.path_prefix, str(settings.photo_root.resolve()))


@app.post("/api/v1/assets/{asset_id}/external", status_code=201)
def map_external_asset(asset_id: int, request: ExternalAssetRequest) -> dict:
    return service.map_external_asset(asset_id, request.provider, request.external_id)


@app.post("/api/v1/smart-albums/{smart_album_id}/materialize")
def materialize_smart_album(smart_album_id: int) -> dict:
    if not immich_config.url or not immich_config.api_key:
        raise ValueError("IMMICH_URL and IMMICH_API_KEY must be configured")
    return service.materialize_smart_album(smart_album_id, ImmichClient(ImmichSettings(immich_config.url, immich_config.api_key)))


@app.exception_handler(LookupError)
def missing_record(request, error):
    return JSONResponse(status_code=404, content={"detail": str(error)})


@app.exception_handler(ValueError)
def invalid_value(request, error):
    return JSONResponse(status_code=400, content={"detail": str(error)})


@app.exception_handler(ImmichError)
def remote_failure(request, error):
    return JSONResponse(status_code=502, content={"detail": str(error)})


@app.get("/api/v1/tasks")
def task_status() -> dict:
    return service.task_status()


@app.get("/api/v1/smart-albums")
def smart_albums() -> list[dict]:
    return service.list_smart_albums()


@app.patch("/api/v1/events/{event_id}")
def review_event(event_id: int, request: ReviewRequest) -> dict:
    return service.review_event(event_id, request.state, request.name)


@app.get("/api/v1/tag-assignments")
def tag_assignments(state: Literal["pending", "confirmed", "rejected"] | None = None, offset: int = Query(default=0, ge=0), limit: int = Query(default=100, ge=1, le=200)) -> list[dict]:
    return service.list_tag_assignments(state, offset, limit)


@app.patch("/api/v1/assets/{asset_id}/tags/{tag_id}")
def review_tag(asset_id: int, tag_id: int, request: ReviewRequest) -> dict:
    return service.review_tag(asset_id, tag_id, request.state)


@app.get("/api/v1/assets/{asset_id}/thumbnail")
def thumbnail(asset_id: int):
    rows = database.fetch_all("SELECT path FROM assets WHERE id=? AND missing_at IS NULL", [asset_id])
    if not rows:
        raise HTTPException(404, "photo not found")
    path = Path(rows[0]["path"]).resolve()
    if not path.is_relative_to(settings.photo_root.resolve()):
        raise HTTPException(403, "photo outside configured library")
    try:
        with Image.open(path) as source:
            source.draft("RGB", (480, 480))
            source.thumbnail((480, 480))
            image = ImageOps.exif_transpose(source).convert("RGB")
            output = BytesIO()
            image.save(output, format="JPEG", quality=75)
        return Response(output.getvalue(), media_type="image/jpeg", headers={"Cache-Control": "private, max-age=300"})
    except (OSError, ValueError, Image.DecompressionBombError):
        raise HTTPException(404, "preview unavailable")


static_root = Path(__file__).parent / "static"
app.mount("/static", StaticFiles(directory=static_root), name="static")


@app.get("/", include_in_schema=False)
def index():
    return FileResponse(static_root / "index.html")


class PersonCorrectionRequest(BaseModel):
    person_id: int = Field(gt=0)


@app.patch("/api/v1/people/{person_id}")
def rename_person(person_id: int, request: NamedRequest) -> dict:
    return service.rename_person(person_id, request.name)


@app.get("/api/v1/person-assignments")
def person_assignments(state: Literal["pending", "confirmed", "rejected"] | None = None, offset: int = Query(default=0, ge=0), limit: int = Query(default=100, ge=1, le=200), person_id: int | None = None) -> list[dict]:
    return service.list_person_assignments(state, offset, limit, person_id)


@app.patch("/api/v1/assets/{asset_id}/people/{person_id}")
def review_person(asset_id: int, person_id: int, request: ReviewRequest) -> dict:
    return service.review_person(asset_id, person_id, request.state)


@app.post("/api/v1/assets/{asset_id}/people/{person_id}/correct")
def correct_person(asset_id: int, person_id: int, request: PersonCorrectionRequest) -> dict:
    return service.correct_person(asset_id, person_id, request.person_id)
