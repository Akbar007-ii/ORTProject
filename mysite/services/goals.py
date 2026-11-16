"""Goal, Score ROI, forecast and plan: the database side of services/planning.py."""

import hashlib
import uuid
from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, date, datetime, time

from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from mysite.core.errors import AppError, conflict, not_found, unprocessable
from mysite.db.models import (
    Attempt,
    Checkpoint,
    CheckpointKind,
    ExamFormat,
    ExamFormatVersion,
    GoalMilestone,
    LearningPlan,
    PlanItem,
    PlanItemKind,
    PlanItemStatus,
    PublishStatus,
    QuestionGroup,
    QuestionVersion,
    ScaleKind,
    ScorePrediction,
    ScoreScale,
    Skill,
    SkillProgress,
    SkillSectionWeight,
    Test,
    TestKind,
    TestQuestion,
    University,
    UniversityProgram,
    User,
    UserGoal,
)
from mysite.db.schema.learning import (
    GoalIn,
    GoalOut,
    GoalProgressOut,
    MilestoneOut,
    PlanDayOut,
    PlanItemOut,
    PlanOut,
    PredictionOut,
    ProgramRef,
    RoiItem,
    SkillWeightIn,
    TodayOut,
)
from mysite.services import audit, learning, mastery, planning
from mysite.services import review as review_service


@dataclass
class Context:
    """Everything the maths needs about one student and one exam format."""

    format_version: ExamFormatVersion
    scale: ScoreScale
    tables: dict[uuid.UUID, list[tuple[int, float]]]  # section id -> scale points
    weights: dict[uuid.UUID, dict[uuid.UUID, float]]  # section id -> skill id -> expected questions
    skills: dict[uuid.UUID, Skill]
    progress: dict[uuid.UUID, SkillProgress]
    difficulty: dict[uuid.UUID, tuple[float, float]]  # skill id -> (avg difficulty, avg seconds)
    checkpoint: Checkpoint | None
    priors: dict[str, float]  # section code -> accuracy at the last checkpoint

    @property
    def max_score(self) -> float:
        return sum(max(scaled for _, scaled in table) for table in self.tables.values())

    def prior(self, section_code: str) -> float:
        return self.priors.get(section_code, self.priors.get("", mastery.DEFAULT_PRIOR))

    def mastery_of(self, skill_id: uuid.UUID, section_code: str) -> float:
        progress = self.progress.get(skill_id)
        prior = self.prior(section_code)
        return (
            mastery.mastery(progress.recent_correct, progress.recent_total, prior)
            if progress
            else round(prior, 3)
        )


# ---------- context ----------


async def _weights(db: AsyncSession, fv: ExamFormatVersion) -> dict[uuid.UUID, dict[uuid.UUID, float]]:
    section_ids = [s.id for s in fv.sections]
    result: dict[uuid.UUID, dict[uuid.UUID, float]] = defaultdict(dict)
    rows = await db.scalars(
        select(SkillSectionWeight).where(SkillSectionWeight.exam_section_id.in_(section_ids))
    )
    for row in rows.all():
        result[row.exam_section_id][row.skill_id] = row.expected_questions
    if result:
        return result

    # Experts have not set weights yet: estimate them from how the published mock exams are built.
    mock_count = await db.scalar(
        select(func.count())
        .select_from(Test)
        .where(
            Test.format_version_id == fv.id,
            Test.kind == TestKind.MOCK,
            Test.status == PublishStatus.PUBLISHED,
        )
    )
    if not mock_count:
        return result
    counted = await db.execute(
        select(TestQuestion.section_id, QuestionGroup.skill_id, func.count())
        .join(Test, Test.id == TestQuestion.test_id)
        .join(QuestionVersion, QuestionVersion.id == TestQuestion.question_version_id)
        .join(QuestionGroup, QuestionGroup.id == QuestionVersion.group_id)
        .where(
            Test.format_version_id == fv.id,
            Test.kind == TestKind.MOCK,
            Test.status == PublishStatus.PUBLISHED,
        )
        .group_by(TestQuestion.section_id, QuestionGroup.skill_id)
    )
    for section_id, skill_id, count in counted.all():
        result[section_id][skill_id] = count / mock_count
    return result


async def _last_checkpoint(db: AsyncSession, user_id: uuid.UUID) -> Checkpoint | None:
    return await db.scalar(
        select(Checkpoint)
        .where(Checkpoint.user_id == user_id, Checkpoint.total_scaled.is_not(None))
        .order_by(Checkpoint.taken_at.desc())
        .limit(1)
    )


async def _format_version(db: AsyncSession, user: User, format_code: str | None) -> ExamFormatVersion:
    if format_code:
        fv = await db.scalar(
            select(ExamFormatVersion)
            .join(ExamFormat)
            .where(ExamFormat.code == format_code, ExamFormatVersion.status == PublishStatus.PUBLISHED)
        )
        if fv is None:
            raise unprocessable("invalid_format", "Exam format not found")
        return fv
    goal = await active_goal(db, user.id)
    checkpoint = await _last_checkpoint(db, user.id)
    fv_id = (goal.format_version_id if goal else None) or (
        checkpoint.format_version_id if checkpoint else None
    )
    if fv_id is None:
        published = (
            (
                await db.scalars(
                    select(ExamFormatVersion).where(ExamFormatVersion.status == PublishStatus.PUBLISHED)
                )
            )
            .unique()
            .all()
        )
        if len(published) != 1:
            raise unprocessable("baseline_required", "Take a mock exam first")
        return published[0]
    return (await db.scalars(select(ExamFormatVersion).where(ExamFormatVersion.id == fv_id))).unique().one()


async def load_context(db: AsyncSession, user: User, format_code: str | None = None) -> Context:
    fv = await _format_version(db, user, format_code)
    scale = await db.scalar(
        select(ScoreScale).where(
            ScoreScale.format_version_id == fv.id, ScoreScale.status == PublishStatus.PUBLISHED
        )
    )
    if scale is None:
        raise unprocessable("no_scale", "This exam format has no score scale yet")
    tables: dict[uuid.UUID, list[tuple[int, float]]] = defaultdict(list)
    for point in scale.points:
        tables[point.section_id].append((point.raw_value, point.scaled_value))

    weights = await _weights(db, fv)
    skill_ids = {skill_id for per_section in weights.values() for skill_id in per_section}
    skills = {s.id: s for s in (await db.scalars(select(Skill).where(Skill.id.in_(skill_ids)))).all()}
    progress = {
        p.skill_id: p
        for p in (
            await db.scalars(
                select(SkillProgress)
                .where(SkillProgress.user_id == user.id)
                .execution_options(populate_existing=True)
            )
        ).all()
    }
    stats = await db.execute(
        select(
            QuestionGroup.skill_id,
            func.avg(QuestionVersion.difficulty),
            func.avg(QuestionVersion.estimated_time_sec),
        )
        .join(QuestionVersion, QuestionVersion.group_id == QuestionGroup.id)
        .where(QuestionGroup.skill_id.in_(skill_ids), QuestionVersion.status == "published")
        .group_by(QuestionGroup.skill_id)
    )
    difficulty = {
        skill_id: (float(avg_difficulty or 3), float(avg_sec or planning.DEFAULT_QUESTION_SEC))
        for skill_id, avg_difficulty, avg_sec in stats.all()
    }

    checkpoint = await _last_checkpoint(db, user.id)
    priors: dict[str, float] = {}
    if checkpoint:
        for section in checkpoint.sections:
            if section.get("max"):
                priors[section["code"]] = section["raw"] / section["max"]
        if checkpoint.max_raw:
            priors[""] = checkpoint.total_raw / checkpoint.max_raw
        elif checkpoint.total_scaled is not None:
            # Self-reported score: only the total is known, so every section gets the same share.
            total_max = sum(max(scaled for _, scaled in table) for table in tables.values())
            priors[""] = min(1.0, checkpoint.total_scaled / total_max) if total_max else mastery.DEFAULT_PRIOR
    return Context(fv, scale, dict(tables), weights, skills, progress, difficulty, checkpoint, priors)


# ---------- ROI ----------


def _skill_inputs(ctx: Context) -> list[planning.SkillInput]:
    inputs = []
    for section in ctx.format_version.sections:
        value = planning.point_value(ctx.tables[section.id])
        for skill_id, expected in ctx.weights.get(section.id, {}).items():
            skill = ctx.skills.get(skill_id)
            if skill is None or not skill.active:
                continue
            avg_difficulty, avg_sec = ctx.difficulty.get(skill_id, (3.0, planning.DEFAULT_QUESTION_SEC))
            inputs.append(
                planning.SkillInput(
                    skill_id=skill_id,
                    section_code=section.code,
                    expected_questions=expected,
                    mastery=ctx.mastery_of(skill_id, section.code),
                    point_value=value,
                    difficulty=avg_difficulty,
                    question_sec=avg_sec,
                )
            )
    return inputs


def ranked_skills(ctx: Context) -> list[planning.SkillRoi]:
    return planning.rank(_skill_inputs(ctx))


async def roi(db: AsyncSession, user: User) -> list[RoiItem]:
    ctx = await load_context(db, user)
    inputs = _skill_inputs(ctx)
    items = []
    for item in ranked_skills(ctx):
        skill = ctx.skills[item.skill_id]
        own = [i for i in inputs if i.skill_id == item.skill_id]
        main = next(i for i in own if i.section_code == item.section_code)
        progress = ctx.progress.get(item.skill_id)
        items.append(
            RoiItem(
                skill_id=skill.id,
                skill_code=skill.code,
                title_ky=skill.title_ky,
                title_ru=skill.title_ru,
                section_code=item.section_code,
                mastery=main.mastery,
                answers=progress.attempts if progress else 0,
                # Across every section the skill appears in.
                expected_questions=round(sum(i.expected_questions for i in own), 2),
                potential_gain=item.gain,
                minutes=item.minutes,
                roi=item.roi,
            )
        )
    return items


# ---------- forecast ----------


async def _forecast(db: AsyncSession, user: User, ctx: Context) -> PredictionOut:
    if ctx.checkpoint is None:
        return PredictionOut(available=False, reason="no_checkpoint")
    now = datetime.now(UTC)
    sections = [
        planning.SectionInput(
            code=section.code,
            question_count=section.question_count,
            scale=ctx.tables[section.id],
            prior=ctx.prior(section.code),
            skills=[
                (expected, ctx.mastery_of(skill_id, section.code))
                for skill_id, expected in ctx.weights.get(section.id, {}).items()
            ],
        )
        for section in ctx.format_version.sections
    ]
    answers_since = await db.scalar(
        select(func.count())
        .select_from(Attempt)
        .where(Attempt.user_id == user.id, Attempt.answered_at > ctx.checkpoint.taken_at)
    )
    result = planning.forecast(sections, (now - ctx.checkpoint.taken_at).days, answers_since or 0)
    return PredictionOut(
        available=True,
        low=result.low,
        point=result.point,
        high=result.high,
        confidence=result.confidence,
        is_estimated_scale=ctx.scale.kind == ScaleKind.ESTIMATED,
        based_on_checkpoint_at=ctx.checkpoint.taken_at,
        algorithm_version=planning.ALGORITHM_VERSION,
    )


async def prediction(db: AsyncSession, user: User) -> PredictionOut:
    try:
        ctx = await load_context(db, user)
    except AppError as exc:
        if exc.status_code != 422:
            raise
        return PredictionOut(available=False, reason=exc.code)  # "no forecast yet" is a normal state
    result = await _forecast(db, user, ctx)
    if result.available:
        # One stored forecast per day: enough to draw the trend and to calibrate later.
        today = learning.local_today(user)
        exists = await db.scalar(
            select(ScorePrediction.id).where(
                ScorePrediction.user_id == user.id, ScorePrediction.computed_on == today
            )
        )
        if not exists:
            db.add(
                ScorePrediction(
                    user_id=user.id,
                    low=result.low,
                    point=result.point,
                    high=result.high,
                    confidence=result.confidence,
                    algorithm_version=planning.ALGORITHM_VERSION,
                    computed_on=today,
                )
            )
            await db.commit()
    return result


# ---------- goal ----------


async def active_goal(db: AsyncSession, user_id: uuid.UUID) -> UserGoal | None:
    return await db.scalar(select(UserGoal).where(UserGoal.user_id == user_id, UserGoal.status == "active"))


async def _goal_out(db: AsyncSession, user: User, goal: UserGoal, ctx: Context) -> GoalOut:
    rows = await db.scalars(
        select(GoalMilestone).where(GoalMilestone.goal_id == goal.id).order_by(GoalMilestone.score)
    )
    return GoalOut(
        id=goal.id,
        format_code=ctx.format_version.format.code,
        start_score=goal.start_score,
        target_score=goal.target_score,
        max_score=ctx.max_score,
        exam_date=goal.exam_date,
        days_left=max(0, (goal.exam_date - learning.local_today(user)).days),
        minutes_per_day=goal.minutes_per_day,
        milestones=[MilestoneOut(score=m.score, reached_at=m.reached_at) for m in rows.all()],
        program=await _program_ref(db, goal.university_program_id),
        created_at=goal.created_at,
    )


async def _program_ref(db: AsyncSession, program_id: uuid.UUID | None) -> ProgramRef | None:
    if program_id is None:
        return None
    row = (
        await db.execute(
            select(UniversityProgram, University)
            .join(University, University.id == UniversityProgram.university_id)
            .where(UniversityProgram.id == program_id)
        )
    ).first()
    if row is None:
        return None
    program, university = row
    return ProgramRef(
        id=program.id,
        university_ky=university.title_ky,
        university_ru=university.title_ru,
        title_ky=program.title_ky,
        title_ru=program.title_ru,
        year=program.year,
        threshold_score=program.threshold_score,
        source=program.source,
    )


async def set_goal(db: AsyncSession, user: User, data: GoalIn) -> GoalOut:
    ctx = await load_context(db, user, data.format_code)
    if ctx.checkpoint is None:
        raise unprocessable("baseline_required", "Take a mock exam first, or enter your last mock score")
    target = data.target_score
    if data.program_id:
        program = await db.get(UniversityProgram, data.program_id)
        if program is None or not program.active:
            raise unprocessable("invalid_program", "University program not found")
        # The past threshold is the default target; a student may aim higher.
        target = max(target or 0, program.threshold_score)
    forecast = await _forecast(db, user, ctx)
    start = forecast.point if forecast.available else ctx.checkpoint.total_scaled
    today = learning.local_today(user)
    if data.exam_date <= today:
        raise unprocessable("invalid_exam_date", "The exam date must be in the future")
    if target > ctx.max_score:
        raise unprocessable("target_too_high", f"The maximum score is {ctx.max_score:g}")
    if target <= start:
        raise unprocessable("target_too_low", f"Your current score is already about {start:g}")

    # The previous goal is kept as history, so progress is always shown against what was promised.
    await db.execute(
        update(UserGoal)
        .where(UserGoal.user_id == user.id, UserGoal.status == "active")
        .values(status="archived")
    )
    await db.execute(
        update(LearningPlan)
        .where(LearningPlan.user_id == user.id, LearningPlan.status == "active")
        .values(status="superseded")
    )
    goal = UserGoal(
        id=uuid.uuid4(),
        user_id=user.id,
        format_version_id=ctx.format_version.id,
        start_score=start,
        target_score=target,
        exam_date=data.exam_date,
        minutes_per_day=data.minutes_per_day,
        university_program_id=data.program_id,
    )
    db.add(goal)
    await db.flush()
    db.add_all(GoalMilestone(goal_id=goal.id, score=score) for score in planning.milestones(start, target))
    await db.commit()
    return await _goal_out(db, user, goal, ctx)


async def _require_goal(db: AsyncSession, user: User) -> UserGoal:
    goal = await active_goal(db, user.id)
    if goal is None:
        raise not_found("No active goal")
    return goal


async def get_goal(db: AsyncSession, user: User) -> GoalOut:
    return await _goal_out(db, user, await _require_goal(db, user), await load_context(db, user))


async def archive_goal(db: AsyncSession, user: User) -> None:
    goal = await _require_goal(db, user)
    goal.status = "archived"
    await db.execute(
        update(LearningPlan)
        .where(LearningPlan.goal_id == goal.id, LearningPlan.status == "active")
        .values(status="superseded")
    )
    await db.commit()


async def progress(db: AsyncSession, user: User) -> GoalProgressOut:
    goal = await _require_goal(db, user)
    ctx = await load_context(db, user)
    forecast = await _forecast(db, user, ctx)
    goal_out = await _goal_out(db, user, goal, ctx)
    today = learning.local_today(user)

    current = (
        forecast.point if forecast.available else (ctx.checkpoint.total_scaled if ctx.checkpoint else None)
    )
    started_on = learning.local_today(user, goal.created_at)
    state, expected = planning.track(
        goal.start_score, goal.target_score, started_on, goal.exam_date, today, current
    )
    gap = max(0.0, goal.target_score - current) if current is not None else None
    minutes_needed, max_gain = planning.effort_to_close(gap or 0.0, ranked_skills(ctx))
    available = goal.minutes_per_day * goal_out.days_left
    max_reachable = min(ctx.max_score, (current or 0.0) + max_gain)
    return GoalProgressOut(
        goal=goal_out,
        current_score=current,
        gap=gap,
        next_milestone=next((m.score for m in goal_out.milestones if m.reached_at is None), None),
        track=state,
        expected_score_today=expected,
        # Honest answer to "can I make it?": enough points to win and enough time to win them.
        feasible=max_reachable >= goal.target_score and minutes_needed <= available,
        minutes_needed=minutes_needed,
        minutes_available=available,
        max_reachable=round(max_reachable, 1),
        prediction=forecast,
    )


async def on_checkpoint(db: AsyncSession, user_id: uuid.UUID, checkpoint: Checkpoint) -> None:
    """Called inside the transaction that creates a checkpoint: milestones are earned only by
    measured results, never by a forecast."""
    await learning.award(db, user_id, "first_mock")
    goal = await active_goal(db, user_id)
    if goal is None or checkpoint.total_scaled is None:
        return
    rows = await db.scalars(
        select(GoalMilestone).where(
            GoalMilestone.goal_id == goal.id,
            GoalMilestone.reached_at.is_(None),
            GoalMilestone.score <= checkpoint.total_scaled,
        )
    )
    for milestone in rows.all():
        milestone.reached_at = checkpoint.taken_at
        await learning.award(
            db, user_id, "milestone", f"{goal.id}:{milestone.score:g}", {"score": milestone.score}
        )
    if checkpoint.total_scaled >= goal.target_score:
        await learning.award(db, user_id, "goal_reached", str(goal.id), {"score": checkpoint.total_scaled})


async def add_self_reported(db: AsyncSession, user: User, total_scaled: float, taken_on: date) -> Checkpoint:
    """A mock exam taken elsewhere. Marked as self-reported and shown differently on the chart."""
    if taken_on > learning.local_today(user):
        raise unprocessable("invalid_date", "The date cannot be in the future")
    fv = await _format_version(db, user, None)
    checkpoint = Checkpoint(
        id=uuid.uuid4(),
        user_id=user.id,
        kind=CheckpointKind.SELF_REPORTED,
        format_version_id=fv.id,
        total_raw=0,
        max_raw=0,
        total_scaled=total_scaled,
        sections=[],
        taken_at=datetime.combine(taken_on, time(12), tzinfo=UTC),
    )
    db.add(checkpoint)
    await db.commit()
    return checkpoint


# ---------- plan ----------


def _inputs_hash(goal: UserGoal, ranked: list[planning.SkillRoi], today: date) -> str:
    """The plan is rebuilt when the goal changes, the best skills change, or a new week starts."""
    week = today.isocalendar()
    parts = [
        str(goal.id),
        str(goal.target_score),
        str(goal.exam_date),
        str(goal.minutes_per_day),
        f"{week.year}-{week.week}",
    ]
    parts += [str(item.skill_id) for item in ranked[:5]]
    return hashlib.sha256("|".join(parts).encode()).hexdigest()


async def _ensure_plan(db: AsyncSession, user: User, *, force: bool = False) -> LearningPlan:
    goal = await _require_goal(db, user)
    ctx = await load_context(db, user)
    ranked = ranked_skills(ctx)
    today = learning.local_today(user)
    if goal.exam_date <= today:
        raise conflict("goal_expired", "The exam date has passed: set a new goal")
    inputs_hash = _inputs_hash(goal, ranked, today)

    active = await db.scalar(
        select(LearningPlan)
        .where(LearningPlan.goal_id == goal.id, LearningPlan.status == "active")
        .with_for_update()
    )
    if active and active.inputs_hash == inputs_hash and not force:
        return active

    carried: list[PlanItem] = []
    version = 1
    if active:
        # Work already done today moves to the new plan; the rest of the old plan becomes history.
        carried = list(
            (
                await db.scalars(
                    select(PlanItem).where(
                        PlanItem.plan_id == active.id,
                        PlanItem.day == today,
                        PlanItem.status != PlanItemStatus.PENDING,
                    )
                )
            ).all()
        )
        active.status = "superseded"
        version = active.version + 1
        await db.flush()

    mock_minutes = round(sum(s.time_limit_sec for s in ctx.format_version.sections) / 60)
    used_today = sum(item.minutes for item in carried)
    plan = LearningPlan(
        id=uuid.uuid4(), goal_id=goal.id, user_id=user.id, version=version, inputs_hash=inputs_hash
    )
    db.add(plan)
    await db.flush()
    for item in carried:
        item.plan_id = plan.id
        item.position = -item.position  # keep them first and clear of the new positions
    done_kinds = {item.kind for item in carried}
    for planned in planning.build_plan(
        today, goal.exam_date, goal.minutes_per_day, ranked, mock_minutes, used_today
    ):
        if (
            planned.day == today
            and planned.kind in (PlanItemKind.REVIEW, PlanItemKind.MOCK)
            and planned.kind in done_kinds
        ):
            continue
        db.add(
            PlanItem(
                plan_id=plan.id,
                day=planned.day,
                position=planned.position,
                kind=planned.kind,
                skill_id=planned.skill_id,
                minutes=planned.minutes,
                expected_gain=planned.expected_gain,
            )
        )
    await db.commit()
    return plan


async def _items_out(db: AsyncSession, items: list[PlanItem]) -> list[PlanItemOut]:
    skill_ids = {item.skill_id for item in items if item.skill_id}
    skills = {s.id: s for s in (await db.scalars(select(Skill).where(Skill.id.in_(skill_ids)))).all()}
    return [
        PlanItemOut(
            id=item.id,
            day=item.day,
            kind=PlanItemKind(item.kind),
            skill_id=item.skill_id,
            skill_title_ky=skills[item.skill_id].title_ky if item.skill_id else None,
            skill_title_ru=skills[item.skill_id].title_ru if item.skill_id else None,
            minutes=item.minutes,
            expected_gain=item.expected_gain,
            status=PlanItemStatus(item.status),
        )
        for item in items
    ]


async def _plan_items(db: AsyncSession, plan_id: uuid.UUID, day: date | None = None) -> list[PlanItem]:
    query = select(PlanItem).where(PlanItem.plan_id == plan_id).order_by(PlanItem.day, PlanItem.position)
    if day:
        query = query.where(PlanItem.day == day)
    return list((await db.scalars(query.execution_options(populate_existing=True))).all())


async def get_plan(db: AsyncSession, user: User, *, force: bool = False) -> PlanOut:
    plan = await _ensure_plan(db, user, force=force)
    by_day: dict[date, list[PlanItemOut]] = defaultdict(list)
    for item in await _items_out(db, await _plan_items(db, plan.id)):
        by_day[item.day].append(item)
    return PlanOut(
        id=plan.id,
        version=plan.version,
        days=[
            PlanDayOut(day=day, minutes=sum(i.minutes for i in items), items=items)
            for day, items in sorted(by_day.items())
        ],
    )


async def today(db: AsyncSession, user: User) -> TodayOut:
    plan = await _ensure_plan(db, user)
    day = learning.local_today(user)
    items = await _items_out(db, await _plan_items(db, plan.id, day))
    return TodayOut(
        day=day,
        items=items,
        done=sum(item.status == PlanItemStatus.DONE for item in items),
        total=len(items),
        due_cards=await review_service.due_count(db, user.id),
        streak=mastery.current_streak(await learning.active_days(db, user), day),
    )


async def set_item_status(
    db: AsyncSession, user: User, item_id: uuid.UUID, status: PlanItemStatus
) -> PlanItemOut:
    item = await db.scalar(
        select(PlanItem)
        .join(LearningPlan, LearningPlan.id == PlanItem.plan_id)
        .where(PlanItem.id == item_id, LearningPlan.user_id == user.id)
        .with_for_update(of=PlanItem)
    )
    if item is None:
        raise not_found("Plan item not found")
    item.status = status
    item.completed_at = datetime.now(UTC) if status == PlanItemStatus.DONE else None
    await db.commit()
    return (await _items_out(db, [item]))[0]


async def mark_today_done(db: AsyncSession, user: User, kind: PlanItemKind, now: datetime) -> None:
    """Ticks today's plan item of this kind (e.g. the mock exam the student has just finished)."""
    day = learning.local_today(user, now)
    await db.execute(
        update(PlanItem)
        .where(
            PlanItem.plan_id.in_(
                select(LearningPlan.id).where(
                    LearningPlan.user_id == user.id, LearningPlan.status == "active"
                )
            ),
            PlanItem.day == day,
            PlanItem.kind == kind,
            PlanItem.status == PlanItemStatus.PENDING,
        )
        .values(status=PlanItemStatus.DONE, completed_at=now)
    )


# ---------- admin: skill weights ----------


async def set_weights(db: AsyncSession, fv_id: uuid.UUID, rows: list[SkillWeightIn], actor: User) -> int:
    fv = (
        (await db.scalars(select(ExamFormatVersion).where(ExamFormatVersion.id == fv_id)))
        .unique()
        .one_or_none()
    )
    if fv is None:
        raise not_found("Exam format version not found")
    sections = {s.code: s for s in fv.sections}
    totals: dict[str, float] = defaultdict(float)
    if len({(r.skill_id, r.section_code) for r in rows}) != len(rows):
        raise unprocessable("invalid_weights", "A skill is listed twice in one section")
    known = set((await db.scalars(select(Skill.id).where(Skill.id.in_([r.skill_id for r in rows])))).all())
    for row in rows:
        if row.section_code not in sections:
            raise unprocessable("invalid_section", f"Unknown section: {row.section_code}")
        if row.skill_id not in known:
            raise unprocessable("invalid_skill", f"Skill not found: {row.skill_id}")
        totals[row.section_code] += row.expected_questions
    problems = [
        f"{code}: weights add up to {total:g}, the section has {sections[code].question_count} questions"
        for code, total in totals.items()
        if total > sections[code].question_count + 1e-6
    ]
    if problems:
        raise unprocessable("invalid_weights", "Weights exceed the section size", problems)

    await db.execute(
        delete(SkillSectionWeight).where(SkillSectionWeight.exam_section_id.in_([s.id for s in fv.sections]))
    )
    db.add_all(
        SkillSectionWeight(
            skill_id=row.skill_id,
            exam_section_id=sections[row.section_code].id,
            expected_questions=row.expected_questions,
        )
        for row in rows
    )
    audit.record(db, actor.id, "skill_weights.set", "exam_format_version", fv.id, {"rows": len(rows)})
    await db.commit()
    return len(rows)
