from datetime import datetime, timezone
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class AlbumQuery(BaseModel):
    model_config = ConfigDict(extra='forbid')
    person_ids: list[UUID] = Field(default_factory=list, max_length=20)
    tag_ids: list[UUID] = Field(default_factory=list, max_length=20)
    event: int | None = Field(default=None, gt=0)
    start: datetime | None = None
    end: datetime | None = None
    city: str | None = Field(default=None, max_length=200)
    country: str | None = Field(default=None, max_length=200)
    q: str | None = Field(default=None, max_length=500)
    filename: str | None = Field(default=None, max_length=500)

    @field_validator('start', 'end')
    @classmethod
    def utc(cls, value):
        return value.replace(tzinfo=timezone.utc) if value and value.tzinfo is None else value

    @model_validator(mode='after')
    def dates(self):
        if self.start and self.end and self.start >= self.end:
            raise ValueError('结束时间必须晚于开始时间')
        return self

    def compact(self):
        return {k: v for k, v in self.model_dump(mode='json', exclude_none=True).items() if v != [] and v != ''}


class AlbumRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    name: str = Field(min_length=1, max_length=200)
    query: AlbumQuery
    sync_mode: Literal['append', 'mirror'] = 'mirror'

    @field_validator('name')
    @classmethod
    def trimmed_name(cls, value):
        if not value.strip():
            raise ValueError('名称不能为空')
        return value.strip()


class QueryRequest(BaseModel):
    query: AlbumQuery


class ReviewRequest(BaseModel):
    state: Literal['pending', 'confirmed', 'rejected']
    name: str | None = Field(default=None, min_length=1, max_length=200)
