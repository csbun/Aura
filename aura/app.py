from __future__ import annotations

from fastapi import FastAPI
from pydantic import BaseModel, Field

from .config import Settings
from .db import Database
from .scanner import PhotoScanner
from .service import AuraService
from .events import build_day_candidates
from .query import parse_query

settings = Settings.from_env()
database = Database(settings.db_path)
service = AuraService(database)
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
