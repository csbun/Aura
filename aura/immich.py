"""The only module that knows Immich HTTP routes and response shapes.

Contract: official Immich 3.3.4. No database access or filesystem mounts.
Searches fail closed: malformed/incomplete pagination never becomes an empty album.
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen

SUPPORTED_VERSIONS = frozenset({'3.3.4'})


class ImmichError(RuntimeError):
    pass


@dataclass(frozen=True)
class ImmichSettings:
    base_url: str
    api_key: str
    job_api_key: str = ''

    @property
    def configured(self) -> bool:
        return bool(self.base_url and self.api_key)


def _items(value, label: str) -> list[dict]:
    if not isinstance(value, list) or any(not isinstance(x, dict) or not isinstance(x.get('id'), str) for x in value):
        raise ImmichError(f'Immich {label}: invalid response')
    if len({x['id'] for x in value}) != len(value):
        raise ImmichError(f'Immich {label}: duplicate IDs')
    return value


class ImmichClient:
    def __init__(self, settings: ImmichSettings, timeout: float = 30):
        self.settings, self.timeout = settings, timeout

    def _request(self, method: str, path: str, payload: dict | None = None, *, binary=False):
        if not self.settings.configured:
            raise ImmichError('请配置 IMMICH_URL 和 IMMICH_API_KEY')
        body = json.dumps(payload).encode() if payload is not None else None
        request = Request(f'{self.settings.base_url.rstrip("/")}/api{path}', data=body, method=method,
                          headers={'x-api-key': self.settings.api_key, 'Accept': '*/*' if binary else 'application/json',
                                   **({'Content-Type': 'application/json'} if body is not None else {})})
        try:
            with urlopen(request, timeout=self.timeout) as response:
                raw = response.read()
                if binary:
                    content_type = response.headers.get_content_type()
                    if content_type not in {'image/jpeg', 'image/png', 'image/webp', 'image/avif'}:
                        raise ImmichError('Immich returned an invalid thumbnail')
                    return raw, content_type
                return json.loads(raw) if raw else None
        except HTTPError as error:
            raise ImmichError(f'Immich {method} {path}: HTTP {error.code}（检查 API key 权限及资源是否存在）') from error
        except (URLError, TimeoutError, OSError, ValueError) as error:
            raise ImmichError(f'Immich {method} {path}: 请求失败或响应无效') from error

    def server_version(self) -> str:
        result = self._request('GET', '/server/version')
        if not isinstance(result, dict) or any(type(result.get(k)) is not int for k in ('major', 'minor', 'patch')):
            raise ImmichError('Immich: invalid version response')
        return '.'.join(str(result[k]) for k in ('major', 'minor', 'patch'))

    def require_supported(self) -> str:
        version = self.server_version()
        if version not in SUPPORTED_VERSIONS:
            raise ImmichError(f'Immich {version} 尚未验证；自动任务和写入已暂停。支持：{", ".join(sorted(SUPPORTED_VERSIONS))}')
        return version

    def identity(self) -> str:
        result = self._request('GET', '/users/me')
        if not isinstance(result, dict) or not isinstance(result.get('id'), str):
            raise ImmichError('Immich: invalid user response')
        return result['id']

    def people(self) -> list[dict]:
        rows, seen = [], set()
        for page in range(1, 1001):
            result = self._request('GET', f'/people?size=1000&page={page}&withHidden=true')
            if not isinstance(result, dict) or type(result.get('hasNextPage')) is not bool:
                raise ImmichError('Immich: invalid people pagination')
            batch = _items(result.get('people'), 'people')
            if any(x['id'] in seen for x in batch) or (not batch and result['hasNextPage']):
                raise ImmichError('Immich: people pagination did not advance')
            rows.extend(batch)
            seen.update(x['id'] for x in batch)
            if not result['hasNextPage']:
                return rows
        raise ImmichError('Immich: people pagination limit exceeded')

    def tags(self) -> list[dict]:
        return _items(self._request('GET', '/tags'), 'tags')

    def search(self, query: dict, *, with_exif=False) -> list[dict]:
        filters = {'type': {'eq': 'IMAGE'}, 'isOffline': {'eq': False}, 'trashedAt': {'eq': None}}
        for key, remote in [('person_ids', 'personIds'), ('tag_ids', 'tagIds')]:
            if query.get(key):
                filters[remote] = {'all': query[key]}
        for key in ('city', 'country'):
            if query.get(key):
                filters[key] = {'eq': query[key]}
        if query.get('filename'):
            filters['originalFileName'] = {'like': '%' + query['filename'] + '%'}
        dates = {remote: query[key] for key, remote in [('start', 'gte'), ('end', 'lt')] if query.get(key)}
        if dates:
            filters['takenAt'] = dates
        semantic = bool(query.get('q'))
        payload = {'filter': filters, 'size': 100 if semantic else 1000, 'withExif': with_exif,
                   'withPeople': False, 'withStacked': False}
        if semantic:
            payload['query'] = query['q']
        rows, seen, cursors = [], set(), set()
        for _ in range(1000):
            result = self._request('POST', '/search/smart' if semantic else '/search/metadata', payload)
            assets = result.get('assets') if isinstance(result, dict) else None
            if not isinstance(assets, dict) or 'nextCursor' not in assets:
                raise ImmichError('Immich: invalid search pagination')
            batch = _items(assets.get('items'), 'search')
            if any(x['id'] in seen for x in batch):
                raise ImmichError('Immich: repeated search page; retry later')
            rows.extend(batch)
            seen.update(x['id'] for x in batch)
            # Semantic search is explicitly the first 100 ranked results, not a complete catalog.
            if semantic:
                return rows
            cursor = assets['nextCursor']
            if cursor is None:
                return rows
            if not isinstance(cursor, str) or not cursor or cursor in cursors or not batch:
                raise ImmichError('Immich: search pagination did not advance')
            cursors.add(cursor)
            payload['cursor'] = cursor
        raise ImmichError('Immich: search pagination limit exceeded')

    def thumbnail(self, asset_id: str):
        return self._request('GET', f'/assets/{quote(asset_id, safe="")}/thumbnail?size=thumbnail', binary=True)

    def albums(self) -> list[dict]:
        return _items(self._request('GET', '/albums'), 'albums')

    def create_album(self, name: str, marker: str) -> str:
        self.require_supported()
        result = self._request('POST', '/albums', {'albumName': name, 'description': marker})
        if not isinstance(result, dict) or not isinstance(result.get('id'), str):
            raise ImmichError('Immich: invalid create album response')
        return result['id']

    def album_assets(self, album_id: str) -> set[str]:
        # AlbumResponseDto no longer includes all assets in v3; use the paginated search contract.
        rows, cursor, seen = [], None, set()
        for _ in range(1000):
            payload = {'filter': {'albumIds': {'all': [album_id]}}, 'size': 1000,
                       'withExif': False, 'withPeople': False}
            if cursor:
                payload['cursor'] = cursor
            result = self._request('POST', '/search/metadata', payload)
            assets = result.get('assets') if isinstance(result, dict) else None
            if not isinstance(assets, dict) or 'nextCursor' not in assets:
                raise ImmichError('Immich: invalid album search response')
            batch = _items(assets.get('items'), 'album assets')
            ids = {x['id'] for x in batch}
            if ids & set(rows):
                raise ImmichError('Immich: repeated album page')
            rows.extend(ids)
            cursor = assets['nextCursor']
            if cursor is None:
                return set(rows)
            if not isinstance(cursor, str) or not cursor or cursor in seen or not batch:
                raise ImmichError('Immich: invalid album pagination')
            seen.add(cursor)
        raise ImmichError('Immich: album pagination limit exceeded')

    def change_album_assets(self, album_id: str, ids: set[str], *, remove=False):
        if not ids:
            return
        self.require_supported()
        ordered = sorted(ids)
        for offset in range(0, len(ordered), 500):
            batch = ordered[offset:offset + 500]
            result = self._request('DELETE' if remove else 'PUT', f'/albums/{quote(album_id, safe="")}/assets', {'ids': batch})
            result = _items(result, 'album membership')
            accepted = {x['id'] for x in result if x.get('success') is True or (not remove and x.get('error') == 'duplicate')}
            if set(batch) - accepted:
                raise ImmichError('Immich 未完成相册成员更新，下次自动重试')

    def delete_check_album(self, album_id: str, marker: str):
        self.require_supported()
        if not marker.startswith('Aura compatibility check '):
            raise ValueError('not a compatibility test album')
        album = self._request('GET', f'/albums/{quote(album_id, safe="")}')
        if not isinstance(album, dict) or album.get('description') != marker:
            raise ImmichError('Refusing to delete an album with a different marker')
        self._request('DELETE', f'/albums/{quote(album_id, safe="")}')

    def migrate_person_name(self, person_id: str, name: str) -> str:
        """Used only by the 0.3 upgrade, never by normal UI/worker synchronization."""
        self.require_supported()
        path = f'/people/{quote(person_id, safe="")}'
        person = self._request('GET', path)
        if not isinstance(person, dict) or person.get('id') != person_id or not isinstance(person.get('name'), str):
            raise ImmichError('Immich: invalid migration person response')
        if person['name'] == name:
            return 'done'
        if person['name'] or not name.strip() or name.startswith('未命名人物 '):
            return 'conflict'
        result = self._request('PUT', path, {'name': name})
        if not isinstance(result, dict) or result.get('id') != person_id or result.get('name') != name:
            raise ImmichError('Immich did not confirm migrated person name')
        return 'done'

    @staticmethod
    def _tag(result, expected_id=None):
        if (not isinstance(result, dict) or not isinstance(result.get('id'), str)
                or not isinstance(result.get('name'), str)
                or not isinstance(result.get('value'), str)
                or (expected_id is not None and result['id'] != expected_id)):
            raise ImmichError('Immich: invalid tag response')
        return result

    def tag(self, tag_id: str) -> dict:
        return self._tag(self._request('GET', f'/tags/{quote(tag_id, safe="")}'), tag_id)

    def create_tag(self, name: str, parent_id: str | None = None) -> dict:
        self.require_supported()
        if not name.strip() or '/' in name:
            raise ValueError('标签名称不能为空或包含 /；层级通过父标签指定')
        payload = {'name': name}
        if parent_id:
            payload['parentId'] = parent_id
        result = self._tag(self._request('POST', '/tags', payload))
        if result['name'] != name or result.get('parentId') != parent_id:
            raise ImmichError('Immich did not confirm the requested tag hierarchy')
        return result

    def rename_tag(self, tag_id: str, name: str) -> dict:
        self.require_supported()
        if not name.strip() or '/' in name:
            raise ValueError('标签名称不能为空或包含 /')
        result = self._tag(self._request('PUT', f'/tags/{quote(tag_id, safe="")}', {'name': name}), tag_id)
        if result['name'] != name:
            raise ImmichError('Immich did not confirm the tag name')
        return result

    def change_tag_assets(self, tag_id: str, ids: set[str], *, remove=False):
        if not ids:
            return
        self.require_supported()
        ordered = sorted(ids)
        for offset in range(0, len(ordered), 500):
            batch = ordered[offset:offset + 500]
            result = _items(self._request('DELETE' if remove else 'PUT',
                                         f'/tags/{quote(tag_id, safe="")}/assets', {'ids': batch}), 'tag membership')
            accepted = {r['id'] for r in result if r.get('success') is True
                        or (not remove and r.get('error') == 'duplicate')}
            if set(batch) - accepted:
                raise ImmichError('Immich 未完成标签照片关联更新，不能继续迁移或删除旧数据')

    def delete_tag(self, tag_id: str):
        self.require_supported()
        # A child may have been added manually. Never cascade into an unrelated tag.
        if any(tag.get('parentId') == tag_id for tag in self.tags()):
            raise ImmichError('标签含有子标签，请先处理子标签后再删除')
        self._request('DELETE', f'/tags/{quote(tag_id, safe="")}')

    def asset_detail(self, asset_id: str) -> dict:
        result = self._request('GET', f'/assets/{quote(asset_id, safe="")}')
        if not isinstance(result, dict) or result.get('id') != asset_id or not isinstance(result.get('updatedAt'), str):
            raise ImmichError('Immich: invalid asset detail response')
        _items(result.get('tags'), 'asset tags')
        return result

    def refresh_asset_metadata(self, asset_id: str):
        self.require_supported()
        self._request('POST', '/assets/jobs', {'assetIds': [asset_id], 'name': 'refresh-metadata'})

    def jobs_idle(self, names: tuple[str, ...]) -> bool:
        reader = (ImmichClient(ImmichSettings(self.settings.base_url, self.settings.job_api_key), self.timeout)
                  if self.settings.job_api_key else self)
        try:
            result = reader._request('GET', '/jobs')
        except ImmichError as error:
            raise ImmichError('无法读取 Immich 任务队列：请配置管理员的 IMMICH_JOB_API_KEY（job.read）；照片仍使用原 API key') from error
        if not isinstance(result, dict):
            raise ImmichError('Immich: invalid queue response')
        idle = True
        for name in names:
            queue = result.get(name, {})
            counts = queue.get('jobCounts', {})
            if any(type(counts.get(key)) is not int or counts.get(key, 0) < 0 for key in ('active', 'waiting', 'delayed', 'failed')):
                raise ImmichError(f'Immich: missing queue counts for {name}')
            if counts['failed']:
                raise ImmichError(f'Immich {name} 存在失败任务，请先处理后重试')
            busy = any(counts.get(key, 0) > 0 for key in ('active', 'waiting', 'delayed'))
            if busy and queue.get('queueStatus', {}).get('isPaused'):
                raise ImmichError(f'Immich {name} 队列已暂停，无法完成持久性检查')
            idle = idle and not busy
        return idle

    def wait_tag_writes(self, *, timeout=30, sleep=time.sleep, clock=time.monotonic):
        """Drain the upstream write/read pipeline before another tag mutation.

        HTTP success only confirms the database write; an older sidecar job can
        otherwise overwrite a subsequent edit. Two idle observations also cover
        the transition between sidecar writes and metadata extraction.
        """
        deadline, stable = clock() + timeout, 0
        while stable < 2:
            stable = stable + 1 if self.jobs_idle(('sidecar', 'metadataExtraction')) else 0
            if stable == 2:
                return
            if clock() >= deadline:
                raise ImmichError('Immich 标签写入队列仍忙碌，任务保留并稍后重试')
            sleep(0.25)

    def tag_assets(self, tag_id: str) -> list[dict]:
        """Complete tag membership, including offline assets; never a cached replica."""
        rows, seen, cursors = [], set(), set()
        payload = {'filter': {'tagIds': {'all': [tag_id]}}, 'size': 1000,
                   'withExif': False, 'withPeople': False, 'withStacked': False}
        for _ in range(1000):
            result = self._request('POST', '/search/metadata', payload)
            assets = result.get('assets') if isinstance(result, dict) else None
            if not isinstance(assets, dict) or 'nextCursor' not in assets:
                raise ImmichError('Immich: invalid tag membership pagination')
            batch = _items(assets.get('items'), 'tag assets')
            ids = {a['id'] for a in batch}
            if ids & seen:
                raise ImmichError('Immich: repeated tag membership page')
            rows.extend(batch)
            seen.update(ids)
            cursor = assets['nextCursor']
            if cursor is None:
                return rows
            if not isinstance(cursor, str) or not cursor or cursor in cursors or not batch:
                raise ImmichError('Immich: tag membership pagination did not advance')
            cursors.add(cursor)
            payload['cursor'] = cursor
        raise ImmichError('Immich: tag membership pagination limit exceeded')
