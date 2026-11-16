import uuid
from datetime import datetime
from typing import Annotated

from pydantic import BaseModel, Field, model_validator

from mysite.db.models import Locale, QuestionStatus, QuestionVersion
from mysite.db.schema.auth import InputModel

Stem = Annotated[str, Field(min_length=1, max_length=10_000)]
OptionKey = Annotated[str, Field(pattern=r"^[A-F]$")]


class OptionIn(InputModel):
    key: OptionKey
    text: Annotated[str, Field(min_length=1, max_length=2_000)]
    is_correct: bool = False


def _unique_keys(options: list[OptionIn]) -> list[OptionIn]:
    if len({o.key for o in options}) != len(options):
        raise ValueError("Option keys must be unique")
    return options


Options = Annotated[list[OptionIn], Field(min_length=2, max_length=6)]


class QuestionCreateIn(InputModel):
    # Either a skill (new task) or an existing group (another language of the same task).
    skill_id: uuid.UUID | None = None
    group_id: uuid.UUID | None = None
    language: Locale
    stem: Stem
    options: Options
    solution: Annotated[str, Field(max_length=20_000)] | None = None
    difficulty: Annotated[int, Field(ge=1, le=5)] = 3
    estimated_time_sec: Annotated[int, Field(ge=5, le=3600)] | None = None
    source: Annotated[str, Field(max_length=255)] | None = None
    license: Annotated[str, Field(max_length=64)] | None = None

    @model_validator(mode="after")
    def _check(self) -> "QuestionCreateIn":
        if (self.skill_id is None) == (self.group_id is None):
            raise ValueError("Send exactly one of skill_id or group_id")
        _unique_keys(self.options)
        return self


class QuestionUpdateIn(InputModel):
    stem: Stem | None = None
    options: Options | None = None
    solution: Annotated[str, Field(max_length=20_000)] | None = None
    difficulty: Annotated[int, Field(ge=1, le=5)] | None = None
    estimated_time_sec: Annotated[int, Field(ge=5, le=3600)] | None = None
    source: Annotated[str, Field(max_length=255)] | None = None
    license: Annotated[str, Field(max_length=64)] | None = None

    @model_validator(mode="after")
    def _check(self) -> "QuestionUpdateIn":
        if self.options is not None:
            _unique_keys(self.options)
        return self


class TransitionIn(InputModel):
    to: QuestionStatus
    comment: Annotated[str, Field(max_length=2_000)] | None = None


class OptionAdminOut(BaseModel):
    id: uuid.UUID
    key: str
    text: str
    is_correct: bool


class QuestionAdminOut(BaseModel):
    """Staff view: includes the correct answer and the solution."""

    id: uuid.UUID
    group_id: uuid.UUID
    code: str
    skill_id: uuid.UUID
    version: int
    language: Locale
    stem: str
    options: list[OptionAdminOut]
    solution: str | None
    difficulty: int
    estimated_time_sec: int | None
    source: str | None
    license: str | None
    status: QuestionStatus
    author_id: uuid.UUID | None
    created_at: datetime
    published_at: datetime | None

    @classmethod
    def from_version(cls, v: QuestionVersion) -> "QuestionAdminOut":
        return cls(
            id=v.id,
            group_id=v.group_id,
            code=v.group.code,
            skill_id=v.group.skill_id,
            version=v.version,
            language=Locale(v.language),
            stem=v.stem,
            options=[
                OptionAdminOut(id=o.id, key=o.key, text=o.text, is_correct=o.is_correct) for o in v.options
            ],
            solution=v.solution,
            difficulty=v.difficulty,
            estimated_time_sec=v.estimated_time_sec,
            source=v.source,
            license=v.license,
            status=QuestionStatus(v.status),
            author_id=v.author_id,
            created_at=v.created_at,
            published_at=v.published_at,
        )


class QuestionPage(BaseModel):
    items: list[QuestionAdminOut]
    total: int
    limit: int
    offset: int


class ReviewOut(BaseModel):
    from_status: str
    to_status: str
    actor_id: uuid.UUID | None
    comment: str | None
    created_at: datetime
