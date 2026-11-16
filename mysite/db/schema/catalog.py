import uuid
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field

from mysite.db.schema.auth import InputModel

Code = Annotated[str, Field(min_length=1, max_length=32, pattern=r"^[a-z0-9_\-\.]+$")]
SkillCode = Annotated[str, Field(min_length=1, max_length=64, pattern=r"^[a-z0-9_\-\.]+$")]
Title = Annotated[str, Field(min_length=1, max_length=120)]
LongTitle = Annotated[str, Field(min_length=1, max_length=160)]
Order = Annotated[int, Field(ge=0, le=100_000)]


class OutModel(BaseModel):
    model_config = ConfigDict(from_attributes=True)


class SubjectOut(OutModel):
    id: uuid.UUID
    code: str
    title_ky: str
    title_ru: str
    order: int
    active: bool


class SkillOut(OutModel):
    id: uuid.UUID
    topic_id: uuid.UUID
    code: str
    title_ky: str
    title_ru: str
    order: int
    active: bool


class TopicOut(OutModel):
    id: uuid.UUID
    subject_id: uuid.UUID
    parent_id: uuid.UUID | None
    title_ky: str
    title_ru: str
    order: int
    active: bool


class TopicNode(TopicOut):
    skills: list[SkillOut] = []
    children: list["TopicNode"] = []


class SubjectCreateIn(InputModel):
    code: Code
    title_ky: Title
    title_ru: Title
    order: Order = 0
    active: bool = True


class SubjectUpdateIn(InputModel):
    title_ky: Title | None = None
    title_ru: Title | None = None
    order: Order | None = None
    active: bool | None = None


class TopicCreateIn(InputModel):
    subject_id: uuid.UUID
    parent_id: uuid.UUID | None = None
    title_ky: LongTitle
    title_ru: LongTitle
    order: Order = 0
    active: bool = True


class TopicUpdateIn(InputModel):
    parent_id: uuid.UUID | None = None
    title_ky: LongTitle | None = None
    title_ru: LongTitle | None = None
    order: Order | None = None
    active: bool | None = None


class SkillCreateIn(InputModel):
    topic_id: uuid.UUID
    code: SkillCode
    title_ky: LongTitle
    title_ru: LongTitle
    order: Order = 0
    active: bool = True


class SkillUpdateIn(InputModel):
    topic_id: uuid.UUID | None = None
    title_ky: LongTitle | None = None
    title_ru: LongTitle | None = None
    order: Order | None = None
    active: bool | None = None
