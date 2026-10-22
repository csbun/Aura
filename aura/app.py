from pathlib import Path
from uuid import UUID
import json

from fastapi import FastAPI, Query
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles

from . import __version__
from .config import ImmichConfig, Settings
from .db import Database
from .immich import ImmichClient, ImmichError, ImmichSettings, SUPPORTED_VERSIONS
from .models import AlbumRequest, QueryRequest, ReviewRequest, MergeEventsRequest
from .service import AuraService

settings, immich_config = Settings.from_env(), ImmichConfig.from_env()
database = Database(settings.db_path)
immich = ImmichClient(ImmichSettings(immich_config.url, immich_config.api_key))
service = AuraService(database, immich)
app = FastAPI(title='Aura — Immich 高级整理', version=__version__)


@app.get('/health')
def health():
    return {'status': 'ok', 'version': __version__}


@app.get('/api/v1/config')
def config():
    return {'immich_public_url': immich_config.public_url, 'immich_port': immich_config.port,
            'supported_versions': sorted(SUPPORTED_VERSIONS)}


@app.get('/api/v1/integrations/immich')
def integration():
    if not immich.settings.configured:
        return {'configured': False, 'reachable': False, 'compatible': False}
    try:
        version = immich.server_version()
        return {'configured': True, 'reachable': True, 'compatible': version in SUPPORTED_VERSIONS, 'version': version}
    except ImmichError as error:
        return {'configured': True, 'reachable': False, 'compatible': False, 'error': str(error)}


@app.get('/api/v1/people')
def people():
    return immich.people()


@app.get('/api/v1/tags')
def tags():
    return immich.tags()


@app.get('/api/v1/events')
def events():
    return service.list_events()


@app.post('/api/v1/events/merge')
def merge_events(request: MergeEventsRequest):
    return service.merge_events(request.event_ids, request.name)


@app.delete('/api/v1/events/{event_id}')
def delete_event(event_id: int):
    return service.delete_event(event_id)


@app.patch('/api/v1/events/{event_id}')
def review_event(event_id: int, request: ReviewRequest):
    return service.review_event(event_id, request.state, request.name)


@app.get('/api/v1/events/{event_id}/assets')
def event_assets(event_id: int, offset: int = Query(0, ge=0), limit: int = Query(30, ge=1, le=100)):
    rows = service.search_assets({'event': event_id})
    return {'total': len(rows), 'assets': [{'id': r['id']} for r in rows[offset:offset + limit]]}


@app.post('/api/v1/smart-albums/preview')
def preview(request: QueryRequest):
    return service.preview_album(request.query.compact())


@app.get('/api/v1/smart-albums')
def albums():
    return service.list_smart_albums()


@app.post('/api/v1/smart-albums', status_code=201)
def create_album(request: AlbumRequest):
    return service.save_album(request.name, request.query.compact(), request.sync_mode)


@app.patch('/api/v1/smart-albums/{album_id}')
def update_album(album_id: int, request: AlbumRequest):
    return service.save_album(request.name, request.query.compact(), request.sync_mode, album_id)


@app.post('/api/v1/smart-albums/{album_id}/materialize')
def materialize(album_id: int):
    return service.materialize(album_id)


@app.get('/api/v1/search/parse')
def parse(text: str = Query(min_length=1, max_length=500)):
    return service.parse_search(text)


@app.get('/api/v1/assets/{asset_id}/thumbnail')
def thumbnail(asset_id: UUID):
    content, content_type = immich.thumbnail(str(asset_id))
    return Response(content, media_type=content_type, headers={'Cache-Control': 'private, max-age=300'})


@app.get('/api/v1/tasks')
def tasks():
    rows = database.fetch_all('SELECT * FROM task_status ORDER BY name')
    for row in rows:
        row['result'] = json.loads(row.pop('result_json') or 'null')
    return {'tasks': rows}


@app.get('/api/v1/migration')
def migration():
    return json.loads(database.get_setting('migration_report', 'null'))


@app.exception_handler(LookupError)
def missing(request, error):
    return JSONResponse(status_code=404, content={'detail': str(error)})


@app.exception_handler(ValueError)
def invalid(request, error):
    return JSONResponse(status_code=400, content={'detail': str(error)})


@app.exception_handler(ImmichError)
def upstream(request, error):
    return JSONResponse(status_code=502, content={'detail': str(error)})


static_root = Path(__file__).parent / 'static'
app.mount('/static', StaticFiles(directory=static_root), name='static')


@app.get('/', include_in_schema=False)
def index():
    return FileResponse(static_root / 'index.html')
