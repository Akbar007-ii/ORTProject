import uuid
from datetime import date, datetime
from typing import Annotated, Any

from pydantic import BaseModel, Field, model_validator

from mysite.db.models import CardStatus, MistakeReason, PlanItemKind, PlanItemStatus, TutorMode
from mysite.db.schema.auth import InputModel
from mysite.db.schema.exam import SnapshotOption

# ---------- practice ----------


class PracticeStartIn(InputModel):
    skill_id: uuid.UUID | None = None
    topic_id: uuid.UUID | None = None
    count: Annotated[int, Field(ge=1, le=30)] = 10
    plan_item_id: uuid.UUID | None = None

    @model_validator(mode="after")
    def _one_target(self) -> "PracticeStartIn":
        if (self.skill_id is None) == (self.topic_id is None):
            raise ValueError("Send exactly one of skill_id or topic_id")
        return self


class PracticeAnswerIn(InputModel):
    option_id: uuid.UUID
    time_spent_sec: Annotated[int, Field(ge=0)] = 0


class FeedbackOut(BaseModel):
    """Shown right after an answer: the verified key and solution."""

    is_correct: bool
    selected_option_id: uuid.UUID
    correct_option_id: uuid.UUID
    solution: str | None


class PracticeQuestionOut(BaseModel):
    id: uuid.UUID
    position: int
    question_version_id: uuid.UUID
    skill_id: uuid.UUID
    stem: str
    options: list[SnapshotOption]
    feedback: FeedbackOut | None  # null until answered


class PracticeOut(BaseModel):
    id: uuid.UUID
    status: str
    skill_id: uuid.UUID | None
    topic_id: uuid.UUID | None
    total: int
    answered: int
    correct: int
    questions: list[PracticeQuestionOut]


# ---------- mistakes and review ----------


class CardOut(BaseModel):
    id: uuid.UUID
    status: CardStatus
    source: str
    reason: MistakeReason | None
    skill_id: uuid.UUID
    question_code: str
    stem: str
    options: list[SnapshotOption]
    selected_option_id: uuid.UUID | None
    correct_option_id: uuid.UUID
    solution: str | None
    error_count: int
    consecutive_successes: int
    due_at: datetime | None
    created_at: datetime


class CardPage(BaseModel):
    items: list[CardOut]
    total: int
    limit: int
    offset: int


class CardUpdateIn(InputModel):
    reason: MistakeReason | None = None
    archived: bool | None = None  # true: take the card out of review; false: bring it back


class ReviewCardOut(BaseModel):
    """A due card as a question: no key, no solution."""

    card_id: uuid.UUID
    skill_id: uuid.UUID
    stem: str
    options: list[SnapshotOption]
    error_count: int
    due_at: datetime
    interval_days: int


class ReviewQueueOut(BaseModel):
    due: int
    estimated_minutes: int
    items: list[ReviewCardOut]


class ReviewAnswerIn(InputModel):
    option_id: uuid.UUID
    time_spent_sec: Annotated[int, Field(ge=0)] = 0


class ReviewResultOut(FeedbackOut):
    card_id: uuid.UUID
    status: CardStatus
    consecutive_successes: int
    next_due_at: datetime | None
    interval_days: int


# ---------- goal, roi, prediction, plan ----------


class GoalIn(InputModel):
    # Either a score, or a university program whose past threshold becomes the target.
    target_score: Annotated[float, Field(gt=0, le=1000)] | None = None
    program_id: uuid.UUID | None = None
    exam_date: date
    minutes_per_day: Annotated[int, Field(ge=10, le=600)]
    format_code: str | None = None  # default: the format of the student's last mock exam

    @model_validator(mode="after")
    def _target(self) -> "GoalIn":
        if self.target_score is None and self.program_id is None:
            raise ValueError("Send target_score or program_id")
        return self


class ProgramRef(BaseModel):
    id: uuid.UUID
    university_ky: str
    university_ru: str
    title_ky: str
    title_ru: str
    year: int
    threshold_score: float
    source: str


class MilestoneOut(BaseModel):
    score: float
    reached_at: datetime | None


class RoiItem(BaseModel):
    skill_id: uuid.UUID
    skill_code: str
    title_ky: str
    title_ru: str
    section_code: str
    mastery: float  # 0..1, "level of mastery" heuristic
    answers: int
    expected_questions: float
    potential_gain: float  # exam points if the skill reaches the target level
    minutes: int  # estimated practice time to get there
    roi: float  # points per hour


class PredictionOut(BaseModel):
    available: bool
    reason: str | None = None  # why there is no forecast yet
    low: float | None = None
    point: float | None = None
    high: float | None = None
    confidence: str | None = None
    is_estimated_scale: bool = False
    based_on_checkpoint_at: datetime | None = None
    algorithm_version: int | None = None


class GoalOut(BaseModel):
    id: uuid.UUID
    format_code: str
    start_score: float
    target_score: float
    max_score: float
    exam_date: date
    days_left: int
    minutes_per_day: int
    milestones: list[MilestoneOut]
    program: ProgramRef | None = None
    created_at: datetime


class GoalProgressOut(BaseModel):
    goal: GoalOut
    current_score: float | None  # forecast point, or the last measured score
    gap: float | None
    next_milestone: float | None
    track: str  # ahead | on_track | behind | unknown
    expected_score_today: float
    feasible: bool
    minutes_needed: int
    minutes_available: int
    max_reachable: float
    prediction: PredictionOut


class PlanItemOut(BaseModel):
    id: uuid.UUID
    day: date
    kind: PlanItemKind
    skill_id: uuid.UUID | None
    skill_title_ky: str | None
    skill_title_ru: str | None
    minutes: int
    expected_gain: float
    status: PlanItemStatus


class PlanDayOut(BaseModel):
    day: date
    minutes: int
    items: list[PlanItemOut]


class PlanOut(BaseModel):
    id: uuid.UUID
    version: int
    days: list[PlanDayOut]


class TodayOut(BaseModel):
    day: date
    items: list[PlanItemOut]
    done: int
    total: int
    due_cards: int
    streak: int


class PlanItemUpdateIn(InputModel):
    status: PlanItemStatus


class SelfReportedIn(InputModel):
    total_scaled: Annotated[float, Field(ge=0, le=1000)]
    taken_on: date


class SkillWeightIn(InputModel):
    skill_id: uuid.UUID
    section_code: str
    expected_questions: Annotated[float, Field(gt=0, le=300)]


# ---------- stats ----------


class OverviewOut(BaseModel):
    period_days: int
    solved: int
    accuracy: float | None  # null when nothing was solved: not a misleading 0%
    avg_time_sec: float | None
    due_cards: int
    streak: int
    longest_streak: int
    active_days: int


class SkillStatOut(BaseModel):
    skill_id: uuid.UUID
    skill_code: str
    title_ky: str
    title_ru: str
    answers: int
    accuracy: float
    mastery: float
    mastered: bool
    avg_time_sec: float
    last_answer_at: datetime | None


class TrajectoryPoint(BaseModel):
    taken_at: datetime
    kind: str
    total_scaled: float | None
    total_raw: int
    max_raw: int


class TrajectoryOut(BaseModel):
    points: list[TrajectoryPoint]
    target_score: float | None
    exam_date: date | None
    milestones: list[MilestoneOut]
    prediction: PredictionOut


class AchievementOut(BaseModel):
    code: str
    ref: str
    context: dict[str, Any] | None
    earned_at: datetime


class WeeklyReportOut(BaseModel):
    week_start: date
    solved: int
    solved_previous: int
    accuracy: float | None
    accuracy_previous: float | None
    active_days: int
    plan_done: int
    plan_total: int
    cards_mastered: int
    score_now: float | None
    score_week_ago: float | None
    next_milestone: float | None
    streak: int


# ---------- tutor, reports, telegram ----------


class TutorIn(InputModel):
    question_version_id: uuid.UUID
    mode: TutorMode = TutorMode.SHORT


class TutorOut(BaseModel):
    content: str
    # "ai": written by the model from the verified solution; "verified_solution": the solution itself.
    source: str
    mode: TutorMode
    cached: bool
    remaining_today: int


class ReportIn(InputModel):
    question_version_id: uuid.UUID
    kind: Annotated[str, Field(pattern="^(question|explanation)$")]
    comment: Annotated[str, Field(max_length=2000)] | None = None


class LinkTokenOut(BaseModel):
    token: str
    expires_in: int
    deep_link: str | None
