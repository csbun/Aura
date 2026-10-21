from __future__ import annotations

import json
from dataclasses import dataclass
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen


class ImmichError(RuntimeError):
    pass


@dataclass(frozen=True)
class ImmichSettings:
    base_url: str
    api_key: str

    @property
    def configured(self) -> bool:
        return bool(self.base_url and self.api_key)


class ImmichClient:
    """Small, replaceable adapter around the public Immich HTTP interface."""

    def __init__(self, settings: ImmichSettings, timeout: float = 10.0):
        self.settings = settings
        self.timeout = timeout

    def _request(self, method: str, path: str, payload: dict | None = None):
        if not self.settings.configured:
            raise ImmichError("Immich integration is not configured")
        body = json.dumps(payload).encode() if payload is not None else None
        request = Request(
            f"{self.settings.base_url.rstrip('/')}{path}",
            data=body,
            method=method,
            headers={"Accept": "application/json", "x-api-key": self.settings.api_key, **({"Content-Type": "application/json"} if body else {})},
        )
        try:
            with urlopen(request, timeout=self.timeout) as response:
                raw = response.read()
                return json.loads(raw) if raw else None
        except (HTTPError, URLError, TimeoutError) as error:
            detail = getattr(error, "reason", error)
            raise ImmichError(f"Immich request failed: {method} {path}: {detail}") from error

    def server_info(self) -> dict:
        return self._request("GET", "/api/server/ping")

    def list_albums(self) -> list[dict]:
        result = self._request("GET", "/api/albums")
        return result if isinstance(result, list) else []

    def search_metadata(self, page: int = 1, size: int = 1000) -> list[dict]:
        result = self._request(
            "POST",
            "/api/search/metadata",
            {"page": page, "size": size, "withExif": False, "withPeople": False, "withStacked": False},
        )
        if isinstance(result, dict):
            assets = result.get("assets") or result.get("items") or []
            if isinstance(assets, dict):
                assets = assets.get("items") or assets.get("assets") or assets.get("data") or []
            return assets if isinstance(assets, list) else []
        return result if isinstance(result, list) else []

    def all_assets(self, page_size: int = 1000) -> list[dict]:
        page = 1
        assets: list[dict] = []
        while True:
            batch = self.search_metadata(page=page, size=page_size)
            assets.extend(batch)
            if len(batch) < page_size:
                return assets
            page += 1

    def create_album(self, name: str, asset_ids: list[str] | None = None) -> dict:
        return self._request("POST", "/api/albums", {"albumName": name, "assetIds": asset_ids or []})

    def add_assets(self, album_id: str, asset_ids: list[str]) -> None:
        if asset_ids:
            result = self._request("PUT", f"/api/albums/{quote(album_id, safe='')}/assets", {"ids": asset_ids})
            if not isinstance(result, list) or not all(isinstance(item, dict) for item in result):
                raise ImmichError("unexpected album membership response")
            accepted = {str(item.get("id") or item.get("assetId")) for item in result if item.get("success") is True or item.get("error") == "duplicate"}
            if set(asset_ids) - accepted:
                raise ImmichError("Immich did not accept all album assets; retry scheduled")

    def ensure_album(self, name: str) -> dict:
        for album in self.list_albums():
            if album.get("albumName") == name:
                return album
        return self.create_album(name)

    def asset_info(self, asset_id: str) -> dict:
        result = self._request("GET", f"/api/assets/{quote(asset_id, safe='')}")
        if not isinstance(result, dict) or result.get("id") != asset_id:
            raise ImmichError("unexpected asset detail response")
        # Omitted relationships are not an authoritative empty list.
        for key in ("people", "tags"):
            if key not in result or not isinstance(result[key], list):
                raise ImmichError(f"asset detail is missing {key}; check Immich version and API permissions")
            for item in result[key]:
                if not isinstance(item, dict) or not isinstance(item.get("id"), str) or not isinstance(item.get("name"), str):
                    raise ImmichError(f"invalid {key} relationship")
        return result

    def server_version(self) -> str:
        result = self._request("GET", "/api/server/version")
        if not isinstance(result, dict) or not all(isinstance(result.get(key), int) for key in ("major", "minor", "patch")):
            raise ImmichError("unexpected server version response")
        return ".".join(str(result[key]) for key in ("major", "minor", "patch"))

    def face_queues(self) -> dict:
        result = self._request("GET", "/api/jobs")
        if not isinstance(result, dict):
            raise ImmichError("unexpected Immich job response")
        for name in ("faceDetection", "facialRecognition"):
            queue = result.get(name, {})
            if not isinstance(queue, dict) or not isinstance(queue.get("jobCounts"), dict) or not isinstance(queue.get("queueStatus"), dict):
                raise ImmichError(f"missing Immich queue: {name}")
            if not all(isinstance(queue['jobCounts'].get(key), int) for key in ('active','waiting','failed')) or not all(isinstance(queue['queueStatus'].get(key), bool) for key in ('isPaused','isActive')):
                raise ImmichError(f"invalid Immich queue status: {name}")
        return result

    def start_missing_faces(self, queue: str) -> None:
        if queue not in {"faceDetection", "facialRecognition"}:
            raise ValueError("invalid face queue")
        self._request("PUT", f"/api/jobs/{queue}", {"command": "start", "force": False})
