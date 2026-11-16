import uuid
from datetime import datetime
from typing import Annotated, Any

from pydantic import BaseModel, Field, model_validator

from mysite.db.models import Locale, PublishStatus, ScaleKind, TestKind
from mysite.db.schema.auth import InputModel
from mysite.db.schema.catalog import Code, LongTitle, Title

# ---------- admin: exam formats and scales ----------


class SectionIn(InputModel):
    code: Code
    title_ky: Title
    title_ru: Title
    question_count: Annotated[int, Field(ge=1, le=300)]
    time_limit_sec: Annotated[int, Field(ge=60, le=6 * 3600)]


class ExamFormatIn(InputModel):
    """Creates the format if the code is new, otherwise adds the next draft version to it."""

    code: Code
    title_ky: Title
    title_ru: Title
    rules: dict[str, Any] = {}
    sections: Annotated[list[SectionIn], Field(min_length=1, max_length=20)]

    @model_validator(mode="after")
    def _unique_sections(self) -> "ExamFormatIn":
        if len({s.code for s in self.sections}) != len(self.sections):
            raise ValueError("Section codes must be unique")
        return self


class SectionOut(BaseModel):
    id: uuid.UUID
    code: str
    title_ky: str
    title_ru: str
    question_count: int
    time_limit_sec: int
    order: int


class ExamFormatVersionOut(BaseModel):
    id: uuid.UUID
    code: str
    title_ky: str
    title_ru: str
    version: int
    status: PublishStatus
    rules: dict[str, Any]
    sections: list[SectionOut]


class ScalePointIn(InputModel):
    section_code: Code
    raw: Annotated[int, Field(ge=0, le=300)]
    scaled: Annotated[float, Field(ge=0, le=1000)]


class ScoreScaleIn(InputModel):
    kind: ScaleKind = ScaleKind.ESTIMATED
    source: Annotated[str, Field(max_length=255)] | None = None
    points: Annotated[list[ScalePointIn], Field(min_length=2, max_length=5000)]


class ScoreScaleOut(BaseModel):
    id: uuid.UUID
    format_version_id: uuid.UUID
    version: int
    kind: ScaleKind
    status: PublishStatus
    source: str | None


# ---------- admin: tests ----------


class TestQuestionIn(InputModel):
    question_version_id: uuid.UUID
    section_code: Code | None = None
    points: Annotated[int, Field(ge=1, le=10)] = 1


class TestIn(InputModel):
    """Questions are stored in the order given: that is the order students see."""

    code: Code
    title_ky: LongTitle
    title_ru: LongTitle
    kind: TestKind
    language: Locale
    subject_id: uuid.UUID | None = None
    format_version_id: uuid.UUID | None = None
    time_limit_sec: Annotated[int, Field(ge=60, le=6 * 3600)] | None = None
    questions: Annotated[list[TestQuestionIn], Field(max_length=500)] = []

    @model_validator(mode="after")
    def _unique_questions(self) -> "TestIn":
        ids = [q.question_version_id for q in self.questions]
        if len(set(ids)) != len(ids):
            raise ValueError("A question can be used only once in a test")
        return self


class TestAdminOut(BaseModel):
    id: uuid.UUID
    code: str
    version: int
    title_ky: str
    title_ru: str
    kind: TestKind
    language: Locale
    subject_id: uuid.UUID | None
    format_version_id: uuid.UUID | None
    time_limit_sec: int | None
    status: PublishStatus
    question_count: int


class ValidationOut(BaseModel):
    valid: bool
    problems: list[str]


# ---------- student: catalog ----------


class TestSectionOut(BaseModel):
    code: str
    title_ky: str
    title_ru: str
    question_count: int
    time_limit_sec: int


class TestOut(BaseModel):
    id: uuid.UUID
    code: str
    title_ky: str
    title_ru: str
    kind: TestKind
    language: Locale
    subject_id: uuid.UUID | None
    question_count: int
    time_limit_sec: int
    sections: list[TestSectionOut]
    # None: only a raw score. "estimated": points are approximate and must be labelled so.
    scale_kind: ScaleKind | None


# ---------- student: session ----------


class AnswerIn(InputModel):
    option_id: uuid.UUID | None = None  # null clears the answer
    marked: bool | None = None
    # Client counter per question, growing with every change. A replayed or late
    # request with an old seq is ignored, so an offline queue cannot undo newer answers.
    seq: Annotated[int, Field(ge=1, le=2_000_000_000)]
    time_spent_sec: Annotated[int, Field(ge=0)] | None = None


class AnswerOut(BaseModel):
    question_id: uuid.UUID
    option_id: uuid.UUID | None
    marked: bool
    seq: int
    applied: bool  # false: the request was stale and the server state below wins
    answered_at: datetime | None
    server_time: datetime


class SnapshotOption(BaseModel):
    id: uuid.UUID
    key: str
    text: str


class SessionQuestionOut(BaseModel):
    id: uuid.UUID
    position: int
    section_code: str | None
    stem: str
    options: list[SnapshotOption]
    selected_option_id: uuid.UUID | None
    marked: bool
    seq: int


class SessionSectionOut(BaseModel):
    code: str
    title_ky: str
    title_ru: str
    time_limit_sec: int
    started_at: datetime | None
    deadline_at: datetime | None
    closed_at: datetime | None


class SessionOut(BaseModel):
    id: uuid.UUID
    test_id: uuid.UUID
    test_code: str
    title_ky: str
    title_ru: str
    kind: TestKind
    status: str
    finish_reason: str | None
    started_at: datetime
    deadline_at: datetime  # of the open section when there are sections, else of the whole test
    submitted_at: datetime | None
    server_time: datetime  # the client shows the timer relative to this, not to its own clock
    current_section_code: str | None
    sections: list[SessionSectionOut]
    questions: list[SessionQuestionOut]


class SessionListItem(BaseModel):
    id: uuid.UUID
    test_id: uuid.UUID
    test_code: str
    title_ky: str
    title_ru: str
    kind: TestKind
    status: str
    started_at: datetime
    submitted_at: datetime | None
    raw_score: int | None
    max_score: int | None
    scaled_score: float | None


# ---------- student: result ----------


class ResultSectionOut(BaseModel):
    code: str
    title_ky: str
    title_ru: str
    raw_score: int
    max_score: int
    scaled_score: float | None
    correct: int
    wrong: int
    skipped: int


class ResultQuestionOut(BaseModel):
    id: uuid.UUID
    position: int
    section_code: str | None
    question_code: str
    skill_id: uuid.UUID
    stem: str
    options: list[SnapshotOption]
    selected_option_id: uuid.UUID | None
    correct_option_id: uuid.UUID
    is_correct: bool | None  # null = skipped
    solution: str | None
    time_spent_sec: int


class CheckpointOut(BaseModel):
    id: uuid.UUID
    kind: str
    session_id: uuid.UUID | None
    total_raw: int
    max_raw: int
    total_scaled: float | None
    sections: list[dict[str, Any]]
    taken_at: datetime


class ResultOut(BaseModel):
    session_id: uuid.UUID
    test_id: uuid.UUID
    test_code: str
    kind: TestKind
    finish_reason: str
    started_at: datetime
    submitted_at: datetime
    duration_sec: int
    raw_score: int
    max_score: int
    scaled_score: float | None
    scale_kind: ScaleKind | None
    score_is_estimated: bool
    correct: int
    wrong: int
    skipped: int
    sections: list[ResultSectionOut]
    questions: list[ResultQuestionOut]
    checkpoint: CheckpointOut | None
