"""Statistics the student sees. Every number has a definition and a period;
when there is no data the value is null instead of a misleading 0%."""

from datetime import UTC, datetime, time, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from mysite.db.models import (
    Attempt,
    CardStatus,
    Checkpoint,
    GoalMilestone,
    LearningPlan,
    MistakeCard,
    PlanItem,
    PlanItemStatus,
    ScorePrediction,
    Skill,
    SkillProgress,
    User,
    UserAchievement,
)
from mysite.db.schema.learning import (
    AchievementOut,
    MilestoneOut,
    OverviewOut,
    SkillStatOut,
    TrajectoryOut,
    TrajectoryPoint,
    WeeklyReportOut,
)
from mysite.services import goals, learning, mastery
from mysite.services import review as review_service


async def _solved(
    db: AsyncSession, user: User, since: datetime, until: datetime | None = None
) -> tuple[int, int, int]:
    """(answers, correct, seconds) in the period."""
    query = select(
        func.count(),
        func.count().filter(Attempt.is_correct),
        func.coalesce(func.sum(Attempt.time_spent_sec), 0),
    ).where(Attempt.user_id == user.id, Attempt.answered_at >= since)
    if until:
        query = query.where(Attempt.answered_at < until)
    return tuple((await db.execute(query)).one())


async def overview(db: AsyncSession, user: User, period_days: int) -> OverviewOut:
    now = datetime.now(UTC)
    solved, correct, seconds = await _solved(db, user, now - timedelta(days=period_days))
    days = await learning.active_days(db, user)
    today = learning.local_today(user, now)
    return OverviewOut(
        period_days=period_days,
        solved=solved,
        accuracy=round(correct / solved, 3) if solved else None,
        avg_time_sec=round(seconds / solved, 1) if solved else None,
        due_cards=await review_service.due_count(db, user.id, now),
        streak=mastery.current_streak(days, today),
        longest_streak=mastery.longest_streak(days),
        active_days=sum(day > today - timedelta(days=period_days) for day in days),
    )


async def skills(db: AsyncSession, user: User) -> list[SkillStatOut]:
    """Weakest first, so the top of the list is where to practise."""
    rows = await db.execute(
        select(SkillProgress, Skill)
        .join(Skill, Skill.id == SkillProgress.skill_id)
        .where(SkillProgress.user_id == user.id, SkillProgress.attempts > 0)
    )
    result = [
        SkillStatOut(
            skill_id=skill.id,
            skill_code=skill.code,
            title_ky=skill.title_ky,
            title_ru=skill.title_ru,
            answers=progress.attempts,
            accuracy=round(progress.correct / progress.attempts, 3),
            mastery=mastery.mastery(progress.recent_correct, progress.recent_total),
            mastered=mastery.is_mastered(progress.recent_correct, progress.recent_total),
            avg_time_sec=round(progress.total_time_sec / progress.attempts, 1),
            last_answer_at=progress.last_attempt_at,
        )
        for progress, skill in rows.all()
    ]
    return sorted(result, key=lambda item: (item.mastery, -item.answers))


async def trajectory(db: AsyncSession, user: User) -> TrajectoryOut:
    """Measured mock results, the goal line and the current forecast: one chart."""
    checkpoints = await db.scalars(
        select(Checkpoint).where(Checkpoint.user_id == user.id).order_by(Checkpoint.taken_at)
    )
    goal = await goals.active_goal(db, user.id)
    milestones = []
    if goal:
        rows = await db.scalars(
            select(GoalMilestone).where(GoalMilestone.goal_id == goal.id).order_by(GoalMilestone.score)
        )
        milestones = [MilestoneOut(score=m.score, reached_at=m.reached_at) for m in rows.all()]
    return TrajectoryOut(
        points=[
            TrajectoryPoint(
                taken_at=c.taken_at,
                kind=c.kind,
                total_scaled=c.total_scaled,
                total_raw=c.total_raw,
                max_raw=c.max_raw,
            )
            for c in checkpoints.all()
        ],
        target_score=goal.target_score if goal else None,
        exam_date=goal.exam_date if goal else None,
        milestones=milestones,
        prediction=await goals.prediction(db, user),
    )


async def achievements(db: AsyncSession, user: User) -> list[AchievementOut]:
    rows = await db.scalars(
        select(UserAchievement).where(UserAchievement.user_id == user.id).order_by(UserAchievement.earned_at)
    )
    return [
        AchievementOut(code=a.code, ref=a.ref, context=a.context, earned_at=a.earned_at) for a in rows.all()
    ]


async def weekly_report(db: AsyncSession, user: User) -> WeeklyReportOut:
    """This week (Monday to now, in the student's timezone) against the previous one.
    Only the student's own numbers: nobody else is compared."""
    zone = ZoneInfo(user.timezone)
    today = learning.local_today(user)
    week_start = today - timedelta(days=today.weekday())
    start = datetime.combine(week_start, time(), tzinfo=zone)
    previous_start = start - timedelta(days=7)

    solved, correct, _ = await _solved(db, user, start)
    solved_prev, correct_prev, _ = await _solved(db, user, previous_start, start)
    days = await learning.active_days(db, user)

    plan_rows = await db.execute(
        select(PlanItem.status, func.count())
        .join(LearningPlan, LearningPlan.id == PlanItem.plan_id)
        .where(
            LearningPlan.user_id == user.id,
            LearningPlan.status == "active",
            PlanItem.day >= week_start,
            PlanItem.day <= today,
        )
        .group_by(PlanItem.status)
    )
    plan_counts = dict(plan_rows.all())
    mastered = await db.scalar(
        select(func.count())
        .select_from(MistakeCard)
        .where(
            MistakeCard.user_id == user.id,
            MistakeCard.status == CardStatus.MASTERED,
            MistakeCard.updated_at >= start,
        )
    )
    forecast = await goals.prediction(db, user)
    week_ago = await db.scalar(
        select(ScorePrediction.point)
        .where(ScorePrediction.user_id == user.id, ScorePrediction.computed_on <= today - timedelta(days=7))
        .order_by(ScorePrediction.computed_on.desc())
        .limit(1)
    )
    goal = await goals.active_goal(db, user.id)
    next_milestone = None
    if goal:
        next_milestone = await db.scalar(
            select(func.min(GoalMilestone.score)).where(
                GoalMilestone.goal_id == goal.id, GoalMilestone.reached_at.is_(None)
            )
        )
    return WeeklyReportOut(
        week_start=week_start,
        solved=solved,
        solved_previous=solved_prev,
        accuracy=round(correct / solved, 3) if solved else None,
        accuracy_previous=round(correct_prev / solved_prev, 3) if solved_prev else None,
        active_days=sum(day >= week_start for day in days),
        plan_done=plan_counts.get(PlanItemStatus.DONE, 0),
        plan_total=sum(plan_counts.values()),
        cards_mastered=mastered or 0,
        score_now=forecast.point if forecast.available else None,
        score_week_ago=week_ago,
        next_milestone=next_milestone,
        streak=mastery.current_streak(days, today),
    )
