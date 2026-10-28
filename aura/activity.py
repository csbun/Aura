"""Evidence based activity grouping used by the 0.5 event builder.

This module is deliberately pure.  It consumes the public Immich asset shape
and returns groups plus evidence; it does not know about SQLite or files.
"""
from __future__ import annotations

import math
from collections import Counter
from datetime import datetime, timedelta


VISUAL_LABELS = ("会议", "旅行", "聚餐", "风景")


def captured(asset):
    raw = asset.get("localDateTime") or asset.get("fileCreatedAt")
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00")).replace(tzinfo=None) if raw else None
    except (TypeError, ValueError):
        return None


def _exif(asset):
    return asset.get("exifInfo") or asset.get("exif") or {}


def location(asset):
    exif = _exif(asset)
    lat = exif.get("latitude", exif.get("GPSLatitude", asset.get("latitude")))
    lon = exif.get("longitude", exif.get("GPSLongitude", asset.get("longitude")))
    try:
        point = (float(lat), float(lon)) if lat is not None and lon is not None else None
    except (TypeError, ValueError):
        point = None
    label = next((str(exif.get(k) or asset.get(k)).strip() for k in ("city", "state", "country")
                  if exif.get(k) or asset.get(k)), None)
    return point, label


def people(asset):
    rows = asset.get("people") or asset.get("person_ids") or []
    return {str(row.get("id")) if isinstance(row, dict) else str(row) for row in rows if row}


def text(asset):
    exif = _exif(asset)
    return " ".join(str(asset.get(k) or exif.get(k) or "") for k in
                    ("originalFileName", "description", "tags", "tagList"))


def haversine(left, right):
    if not left or not right:
        return None
    lat1, lon1, lat2, lon2 = map(math.radians, (*left, *right))
    a = math.sin((lat2 - lat1) / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin((lon2 - lon1) / 2) ** 2
    return 6371 * 2 * math.asin(math.sqrt(a))


def _visual(asset, visual_index):
    labels = set(visual_index.get(asset.get("id"), set()))
    value = text(asset).lower()
    words = {
        "会议": ("会议", "meeting", "conference", "presentation"),
        "旅行": ("旅行", "travel", "trip", "tourism"),
        "聚餐": ("聚餐", "food", "dinner", "restaurant", "meal"),
        "风景": ("风景", "landscape", "scenery", "mountain", "beach"),
    }
    for label, candidates in words.items():
        if any(word in value for word in candidates):
            labels.add(label)
    return labels


def _edge(left, right, visual_index):
    gap = captured(right) - captured(left)
    if gap < timedelta(0):
        return False, []
    lp, ll = location(left)
    rp, rl = location(right)
    same_label = bool(ll and rl and ll == rl)
    distance = haversine(lp, rp)
    same_place = same_label or (distance is not None and distance <= 35)
    shared_people = people(left) & people(right)
    shared_visual = _visual(left, visual_index) & _visual(right, visual_index)
    reasons = []
    if gap <= timedelta(hours=6):
        if distance is not None and distance > 100 and not shared_people:
            return False, []
        reasons.append("时间连续")
        if same_place:
            reasons.append("地点一致")
        if shared_people:
            reasons.append("人物重合")
        if shared_visual:
            reasons.append("画面主题一致")
        return True, reasons
    # A cross-day activity needs a second, independent signal.  People alone
    # are intentionally insufficient, so recurring people do not merge all of
    # their photos into one event.
    if gap <= timedelta(hours=72) and (same_place or (shared_people and shared_visual)):
        reasons.append("跨日时间连续")
        if same_place:
            reasons.append("地点一致")
        if shared_people:
            reasons.append("人物重合")
        if shared_visual:
            reasons.append("画面主题一致")
        return True, reasons
    return False, []


def _name(items, visual_index):
    dates = [captured(a) for a in items if captured(a)]
    labels = [location(a)[1] for a in items if location(a)[1]]
    kinds = Counter(label for a in items for label in _visual(a, visual_index))
    place = Counter(labels).most_common(1)[0][0] if labels else None
    kind = kinds.most_common(1)[0][0] if kinds else None
    multi_day = bool(dates and dates[-1].date() != dates[0].date())
    if kind == "旅行" or (multi_day and place):
        return f"{place or ''}{kind or '旅行'}".strip()
    if place and kind:
        return f"{place}{kind}"
    if kind:
        return kind
    if place:
        return f"{place}活动"
    return f"{dates[0].month}月{dates[0].day}日活动" if dates else "未命名活动"


def group_activities(assets, visual_index=None):
    visual_index = visual_index or {}
    ordered = sorted((a for a in assets if captured(a)), key=captured)
    groups = []
    evidence = []
    for asset in ordered:
        if not groups:
            groups.append([asset]); evidence.append([]); continue
        joined, reasons = _edge(groups[-1][-1], asset, visual_index)
        if joined:
            groups[-1].append(asset); evidence[-1].extend(reasons)
        else:
            groups.append([asset]); evidence.append([])
    result = []
    for items, reasons in zip(groups, evidence):
        unique = list(dict.fromkeys(reasons))
        independent = sum(r in {"地点一致", "人物重合", "画面主题一致"} for r in unique)
        confidence = min(0.98, 0.5 + 0.12 * independent + (0.12 if len(items) > 1 else 0))
        result.append({"assets": items, "name": _name(items, visual_index),
                       "reasons": unique or ["时间证据有限"], "confidence": confidence,
                       "uncertain": confidence < 0.65})
    return result
