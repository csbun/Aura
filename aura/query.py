from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Iterable


def parse_query(text: str, known_people: Iterable[str] = (), known_tags: Iterable[str] = ()) -> dict:
    """Turn a small, inspectable Chinese query into a search plan.

    This is deliberately a parser, not an LLM. The caller can show the plan
    for confirmation before executing it and a future local model can replace
    this implementation behind the same interface.
    """
    plan: dict = {"text": text, "people": [], "tags": [], "terms": [], "start": None, "end": None, "needs_confirmation": False}
    for person in known_people:
        if person and person in text:
            plan["people"].append(person)
    for tag in known_tags:
        if tag and tag in text:
            plan["tags"].append(tag)
    years = re.findall(r"(20\d{2})\s*年", text)
    if years:
        year = int(years[0])
        plan["start"] = f"{year:04d}-01-01T00:00:00+00:00"
        plan["end"] = f"{year + 1:04d}-01-01T00:00:00+00:00"
    if "去年" in text:
        year = datetime.now(timezone.utc).year - 1
        plan["start"] = f"{year:04d}-01-01T00:00:00+00:00"
        plan["end"] = f"{year + 1:04d}-01-01T00:00:00+00:00"
    remaining = re.sub(r"20\d{2}\s*年|去年|照片|图片|和|的|在|期间", " ", text)
    for word in [*plan["people"], *plan["tags"]]:
        remaining = remaining.replace(word, " ")
    term = " ".join(remaining.split())
    plan["terms"] = [term] if term else []
    plan["needs_confirmation"] = bool(plan["terms"])
    return plan
