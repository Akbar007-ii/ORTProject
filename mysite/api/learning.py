"""Student endpoints of the learning loop: practice, mistakes, review, goal, plan, stats."""

import uuid
from typing import Annotated

from fastapi import APIRouter, Header, Query, Response, status

from mysite.core.deps import DB, CurrentUser
from mysite.db.models import AttemptSource, CardStatus
from mysite.db.schema.exam import CheckpointOut
from mysite.db.schema.learning import (
    AchievementOut,
    CardOut,
    CardPage,
    CardUpdateIn,
    FeedbackOut,
    GoalIn,
    GoalOut,
    GoalProgressOut,
    OverviewOut,
    PlanItemOut,
    PlanItemUpdateIn,
    PlanOut,
    PracticeAnswerIn,
    PracticeOut,
    PracticeStartIn,
    PredictionOut,
    ReviewAnswerIn,
    ReviewQueueOut,
    ReviewResultOut,
    RoiItem,
    SelfReportedIn,
    SkillStatOut,
    TodayOut,
    TrajectoryOut,
    WeeklyReportOut,
)
from mysite.services import goals as goal_service
from mysite.services import practice as practice_service
from mysite.services import review as review_service
from mysite.services import stats as stats_service
from mysite.services.sessions import checkpoint_out

router = APIRouter()

# ---------- practice ----------

practice = APIRouter(prefix="/practice/sessions", tags=["practice"])


@practice.post("", response_model=PracticeOut, status_code=status.HTTP_201_CREATED)
async def start_practice(data: PracticeStartIn, user: CurrentUser, db: DB) -> PracticeOut:
    """Questions on a skill or a topic in the student's language: unseen ones first."""
    return await practice_service.start(db, user, data)


@practice.get("/{session_id}", response_model=PracticeOut)
async def get_practice(session_id: uuid.UUID, user: CurrentUser, db: DB) -> PracticeOut:
    return await practice_service.get(db, user, session_id)


@practice.post("/{session_id}/answers/{question_id}", response_model=FeedbackOut)
async def answer_practice(
    session_id: uuid.UUID, question_id: uuid.UUID, data: PracticeAnswerIn, user: CurrentUser, db: DB
) -> FeedbackOut:
    """Immediate feedback with the verified solution. A wrong answer goes to the mistake journal."""
    return await practice_service.answer(
        db, user, session_id, question_id, data.option_id, data.time_spent_sec
    )


@practice.post("/{session_id}/finish", response_model=PracticeOut)
async def finish_practice(session_id: uuid.UUID, user: CurrentUser, db: DB) -> PracticeOut:
    return await practice_service.finish(db, user, session_id)


# ---------- mistakes and review ----------

mistakes = APIRouter(tags=["mistakes and review"])


@mistakes.get("/mistakes", response_model=CardPage)
async def list_mistakes(
    user: CurrentUser,
    db: DB,
    status_: Annotated[CardStatus | None, Query(alias="status")] = None,
    skill_id: uuid.UUID | None = None,
    source: AttemptSource | None = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> CardPage:
    items, total = await review_service.list_cards(
        db, user, status=status_, skill_id=skill_id, source=source, limit=limit, offset=offset
    )
    return CardPage(items=items, total=total, limit=limit, offset=offset)


@mistakes.patch("/mistakes/{card_id}", response_model=CardOut)
async def update_mistake(card_id: uuid.UUID, data: CardUpdateIn, user: CurrentUser, db: DB) -> CardOut:
    """Set the student's own guess at the reason, or archive / restore the card."""
    return await review_service.update_card(db, user, card_id, data)


@mistakes.get("/review/queue", response_model=ReviewQueueOut)
async def review_queue(user: CurrentUser, db: DB) -> ReviewQueueOut:
    return await review_service.queue(db, user)


@mistakes.post("/review/{card_id}/answer", response_model=ReviewResultOut)
async def review_answer(
    card_id: uuid.UUID,
    data: ReviewAnswerIn,
    user: CurrentUser,
    db: DB,
    idempotency_key: Annotated[str | None, Header(max_length=64)] = None,
) -> ReviewResultOut:
    """Send an Idempotency-Key: a repeated request then returns the same result and
    does not move the card a second time."""
    return await review_service.answer(
        db, user, card_id, data.option_id, data.time_spent_sec, idempotency_key
    )


@mistakes.post("/review/{card_id}/snooze")
async def review_snooze(card_id: uuid.UUID, user: CurrentUser, db: DB) -> dict:
    return {"due_at": await review_service.snooze(db, user, card_id)}


# ---------- goal, roi, forecast, plan ----------

goal = APIRouter(tags=["goal and plan"])


@goal.put("/goals/current", response_model=GoalOut)
async def set_goal(data: GoalIn, user: CurrentUser, db: DB) -> GoalOut:
    """Sets (or replaces) the target score. Needs a mock exam result as the starting point."""
    return await goal_service.set_goal(db, user, data)


@goal.get("/goals/current", response_model=GoalOut)
async def get_goal(user: CurrentUser, db: DB) -> GoalOut:
    return await goal_service.get_goal(db, user)


@goal.delete("/goals/current", status_code=status.HTTP_204_NO_CONTENT)
async def archive_goal(user: CurrentUser, db: DB) -> Response:
    await goal_service.archive_goal(db, user)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@goal.get("/goals/current/progress", response_model=GoalProgressOut)
async def goal_progress(user: CurrentUser, db: DB) -> GoalProgressOut:
    """Where the student is against the goal, and whether the remaining time is enough."""
    return await goal_service.progress(db, user)


@goal.get("/skills/roi", response_model=list[RoiItem])
async def skills_roi(user: CurrentUser, db: DB) -> list[RoiItem]:
    """ "Where the most points are": skills ranked by exam points gained per hour of practice."""
    return await goal_service.roi(db, user)


@goal.get("/prediction", response_model=PredictionOut)
async def prediction(user: CurrentUser, db: DB) -> PredictionOut:
    """A score range with a confidence level. Never a promise, and absent without a mock exam."""
    return await goal_service.prediction(db, user)


@goal.get("/plan", response_model=PlanOut)
async def get_plan(user: CurrentUser, db: DB) -> PlanOut:
    return await goal_service.get_plan(db, user)


@goal.post("/plan/rebuild", response_model=PlanOut)
async def rebuild_plan(user: CurrentUser, db: DB) -> PlanOut:
    return await goal_service.get_plan(db, user, force=True)


@goal.get("/plan/today", response_model=TodayOut)
async def plan_today(user: CurrentUser, db: DB) -> TodayOut:
    return await goal_service.today(db, user)


@goal.patch("/plan/items/{item_id}", response_model=PlanItemOut)
async def update_plan_item(
    item_id: uuid.UUID, data: PlanItemUpdateIn, user: CurrentUser, db: DB
) -> PlanItemOut:
    return await goal_service.set_item_status(db, user, item_id, data.status)


@goal.post("/checkpoints/self-reported", response_model=CheckpointOut, status_code=status.HTTP_201_CREATED)
async def self_reported(data: SelfReportedIn, user: CurrentUser, db: DB) -> CheckpointOut:
    """A mock exam score from outside the platform, as a starting point for the goal."""
    return checkpoint_out(await goal_service.add_self_reported(db, user, data.total_scaled, data.taken_on))


# ---------- stats ----------

stats = APIRouter(tags=["stats"])


@stats.get("/stats/overview", response_model=OverviewOut)
async def stats_overview(
    user: CurrentUser, db: DB, period_days: Annotated[int, Query(ge=1, le=365)] = 30
) -> OverviewOut:
    return await stats_service.overview(db, user, period_days)


@stats.get("/stats/skills", response_model=list[SkillStatOut])
async def stats_skills(user: CurrentUser, db: DB) -> list[SkillStatOut]:
    return await stats_service.skills(db, user)


@stats.get("/stats/score-trajectory", response_model=TrajectoryOut)
async def stats_trajectory(user: CurrentUser, db: DB) -> TrajectoryOut:
    return await stats_service.trajectory(db, user)


@stats.get("/achievements", response_model=list[AchievementOut])
async def achievements(user: CurrentUser, db: DB) -> list[AchievementOut]:
    return await stats_service.achievements(db, user)


@stats.get("/reports/weekly", response_model=WeeklyReportOut)
async def weekly_report(user: CurrentUser, db: DB) -> WeeklyReportOut:
    return await stats_service.weekly_report(db, user)


for part in (practice, mistakes, goal, stats):
    router.include_router(part)
