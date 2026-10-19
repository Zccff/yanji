import math
import uuid
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

MemoryKind = Literal['fact', 'decision', 'task', 'preference', 'repo_context', 'summary']


class EntryFields(BaseModel):
    model_config = ConfigDict(extra='forbid')
    kind: MemoryKind | None = None
    key: str | None = Field(default=None, max_length=240)
    title: str | None = Field(default=None, max_length=500)
    content: str | None = Field(default=None, max_length=20000)
    importance: float | None = Field(default=None, ge=0, le=1)
    confidence: float | None = Field(default=None, ge=0, le=1)
    pinned: bool | None = None
    half_life_days: float | None = Field(default=None, gt=0, le=36500)
    ttl_days: float | None = Field(default=None, ge=0, le=36500)

    @field_validator('content')
    @classmethod
    def nonempty(cls, value):
        if value is not None and not value.strip():
            raise ValueError('content must not be empty')
        return value.strip() if value is not None else value

    @field_validator('importance', 'confidence', 'half_life_days', 'ttl_days')
    @classmethod
    def finite(cls, value):
        if value is not None and not math.isfinite(value):
            raise ValueError('value must be finite')
        return value

    @model_validator(mode='after')
    def reject_null_fields(self):
        for name in self.model_fields_set - {'ttl_days', 'key', 'title'}:
            if getattr(self, name) is None:
                raise ValueError(f'{name} cannot be null')
        return self


class EntryCreate(EntryFields):
    conversation_id: uuid.UUID
    kind: MemoryKind
    content: str = Field(min_length=1, max_length=20000)


class EntryPatch(EntryFields):
    expected_version: int = Field(ge=1)


class ExpectedVersion(BaseModel):
    expected_version: int = Field(ge=1)


class EntryRevert(ExpectedVersion):
    target_version: int = Field(ge=1)


class ConflictResolve(ExpectedVersion):
    resolution: Literal['keep', 'replace']
