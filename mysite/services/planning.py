"""Pure maths of the "start score -> target score" loop: Score ROI, forecast, plan.

No database and no clock here, so every number can be checked by hand in unit tests.
All constants are product hypotheses to be calibrated on the pilot; bump the version
whenever the formulas change, so stored forecasts stay comparable.
"""

import math
import uuid
from dataclasses import dataclass, field
from datetime import date, timedelta

from mysite.services.mastery import TARGET_MASTERY

ALGORITHM_VERSION = 1

QUESTIONS_PER_MASTERY_UNIT = 20  # answers needed to move a skill from 0 to 1
STUDY_OVERHEAD = 2.0  # reading the solution and retrying takes as long as answering
DEFAULT_QUESTION_SEC = 90
MIN_SKILL_MINUTES = 5

SLOT_MINUTES = 15
REVIEW_SHARE = 0.2
MOCK_EVERY_DAYS = 14
FINAL_MOCK_DAYS_BEFORE = 2
PLAN_HORIZON_DAYS = 28
MAX_SKILLS_PER_DAY = 3


# ---------- scale helpers ----------


def scale_value(points: list[tuple[int, float]], raw: float) -> float:
    """Like scoring.scale_lookup, but for a fractional (expected) raw score."""
    points = sorted(points)
    if raw <= points[0][0]:
        return float(points[0][1])
    if raw >= points[-1][0]:
        return float(points[-1][1])
    for (raw_lo, scaled_lo), (raw_hi, scaled_hi) in zip(points, points[1:], strict=False):
        if raw_lo <= raw <= raw_hi:
            return scaled_lo + (raw - raw_lo) / (raw_hi - raw_lo) * (scaled_hi - scaled_lo)
    raise AssertionError("unreachable")


def point_value(points: list[tuple[int, float]]) -> float:
    """Average exam points one more correct answer is worth in this section."""
    points = sorted(points)
    span = points[-1][0] - points[0][0]
    return (points[-1][1] - points[0][1]) / span if span else 0.0


# ---------- Score ROI ----------


@dataclass(frozen=True)
class SkillInput:
    skill_id: uuid.UUID
    section_code: str
    expected_questions: float  # how many questions of the section this skill brings
    mastery: float  # 0..1
    point_value: float  # exam points per correct answer in the section
    difficulty: float = 3.0  # average difficulty of the skill's questions, 1..5
    question_sec: float = DEFAULT_QUESTION_SEC


@dataclass(frozen=True)
class SkillRoi:
    skill_id: uuid.UUID
    section_code: str
    gain: float  # exam points if the skill reaches the target level
    minutes: int  # estimated practice time to get there
    roi: float  # points per hour


def skill_roi(skill: SkillInput) -> SkillRoi:
    gap = max(0.0, TARGET_MASTERY - skill.mastery)
    gain = gap * skill.expected_questions * skill.point_value
    if gap == 0:
        return SkillRoi(skill.skill_id, skill.section_code, 0.0, 0, 0.0)
    questions = math.ceil(gap * QUESTIONS_PER_MASTERY_UNIT)
    harder = 1 + (skill.difficulty - 1) * 0.25
    minutes = max(MIN_SKILL_MINUTES, math.ceil(questions * skill.question_sec / 60 * harder * STUDY_OVERHEAD))
    return SkillRoi(
        skill.skill_id, skill.section_code, round(gain, 2), minutes, round(gain / (minutes / 60), 2)
    )


def rank(skills: list[SkillInput]) -> list[SkillRoi]:
    """Most points per hour first. Skills already at the target level are left out.

    A skill that appears in several exam sections is learned once and pays off in each of
    them: its gains add up, while the practice time is counted once (the longest estimate).
    """
    merged: dict[uuid.UUID, SkillRoi] = {}
    for item in map(skill_roi, skills):
        if item.gain <= 0:
            continue
        seen = merged.get(item.skill_id)
        if seen is None:
            merged[item.skill_id] = item
            continue
        gain = round(seen.gain + item.gain, 2)
        minutes = max(seen.minutes, item.minutes)
        main_section = seen.section_code if seen.gain >= item.gain else item.section_code
        merged[item.skill_id] = SkillRoi(
            item.skill_id, main_section, gain, minutes, round(gain / (minutes / 60), 2)
        )
    return sorted(merged.values(), key=lambda item: (-item.roi, -item.gain, str(item.skill_id)))


def effort_to_close(gap: float, ranked: list[SkillRoi]) -> tuple[int, float]:
    """Minutes of the best-ROI practice needed to gain `gap` points, and the most that can be gained."""
    max_gain = sum(item.gain for item in ranked)
    minutes, gained = 0, 0.0
    for item in ranked:
        if gained >= gap:
            break
        share = min(1.0, (gap - gained) / item.gain)
        minutes += math.ceil(item.minutes * share)
        gained += item.gain * share
    return minutes, round(max_gain, 1)


# ---------- forecast ----------


@dataclass(frozen=True)
class SectionInput:
    code: str
    question_count: int
    scale: list[tuple[int, float]]
    prior: float  # accuracy in this section at the last checkpoint, 0..1
    skills: list[tuple[float, float]] = field(default_factory=list)  # (expected_questions, mastery)


@dataclass(frozen=True)
class Forecast:
    low: float
    point: float
    high: float
    confidence: str


def expected_raw(section: SectionInput) -> float:
    covered = sum(q for q, _ in section.skills)
    # Weights can add up to more than the section holds (rounding, overlapping experts' estimates).
    shrink = min(1.0, section.question_count / covered) if covered else 1.0
    known = sum(q * shrink * m for q, m in section.skills)
    unknown = max(0.0, section.question_count - covered * shrink)
    return known + unknown * section.prior


def forecast(sections: list[SectionInput], checkpoint_age_days: int, answers_since: int) -> Forecast:
    point = sum(scale_value(s.scale, expected_raw(s)) for s in sections)
    max_total = sum(max(scaled for _, scaled in s.scale) for s in sections)

    half = 0.05
    if checkpoint_age_days > 14:
        half += 0.03
    if checkpoint_age_days > 30:
        half += 0.04
    if answers_since >= 50:
        half -= 0.02
    half_width = max_total * half

    if checkpoint_age_days > 30:
        confidence = "low"
    elif checkpoint_age_days <= 14 and answers_since >= 30:
        confidence = "high"
    else:
        confidence = "medium"
    return Forecast(
        low=float(round(max(0.0, point - half_width))),
        point=float(round(point)),
        high=float(round(min(max_total, point + half_width))),
        confidence=confidence,
    )


def milestones(start: float, target: float) -> list[float]:
    """Up to five evenly spaced steps from the start to the target; the last one is the target."""
    gap = target - start
    count = max(1, min(5, round(gap / 20)))
    return sorted({float(round(start + gap * i / count)) for i in range(1, count)} | {float(target)})


def track(
    start: float, target: float, started_on: date, exam_date: date, today: date, current: float | None
) -> tuple[str, float]:
    """Compares the student with a straight line from the start score to the target."""
    total_days = max(1, (exam_date - started_on).days)
    elapsed = min(total_days, max(0, (today - started_on).days))
    expected = start + (target - start) * elapsed / total_days
    if current is None:
        return "unknown", round(expected, 1)
    tolerance = max(1.0, (target - start) * 0.03)
    if current >= expected + tolerance:
        return "ahead", round(expected, 1)
    if current >= expected - tolerance:
        return "on_track", round(expected, 1)
    return "behind", round(expected, 1)


# ---------- plan ----------


@dataclass(frozen=True)
class PlannedItem:
    day: date
    position: int
    kind: str  # practice | review | mock
    skill_id: uuid.UUID | None
    minutes: int
    expected_gain: float


def build_plan(
    today: date,
    exam_date: date,
    minutes_per_day: int,
    ranked: list[SkillRoi],
    mock_minutes: int,
    used_today: int = 0,
) -> list[PlannedItem]:
    """Day-by-day plan for the next weeks.

    Each ordinary day keeps a share for reviewing due cards and spends the rest, in short
    slots, on the skills with the best ROI. Every two weeks, and shortly before the exam,
    the day is a control mock exam instead.
    """
    last_day = min(exam_date - timedelta(days=1), today + timedelta(days=PLAN_HORIZON_DAYS - 1))
    remaining = {item.skill_id: item.minutes for item in ranked}
    by_id = {item.skill_id: item for item in ranked}
    order = [item.skill_id for item in ranked]
    final_mock = exam_date - timedelta(days=FINAL_MOCK_DAYS_BEFORE)
    items: list[PlannedItem] = []

    day, index, cursor = today, 0, 0
    while day <= last_day:
        is_mock_day = (index > 0 and index % MOCK_EVERY_DAYS == MOCK_EVERY_DAYS - 1) or (
            day == final_mock and index > 0
        )
        if is_mock_day:
            items.append(PlannedItem(day, 1, "mock", None, mock_minutes, 0.0))
        else:
            budget = minutes_per_day - (used_today if index == 0 else 0)
            if budget >= 10:
                review = max(5, int(budget * REVIEW_SHARE / 5) * 5)
                items.append(PlannedItem(day, 1, "review", None, review, 0.0))
                budget -= review
                position, todays_skills = 2, []
                while budget >= 10 and order:
                    slot = SLOT_MINUTES if budget >= SLOT_MINUTES + 10 or budget == SLOT_MINUTES else budget
                    if not any(remaining[s] > 0 for s in order):
                        remaining = {s: by_id[s].minutes for s in order}  # everything covered: go round again
                    candidates = todays_skills if len(todays_skills) >= MAX_SKILLS_PER_DAY else order
                    skill_id = None
                    for step in range(len(order)):
                        probe = order[(cursor + step) % len(order)]
                        if remaining[probe] > 0 and probe in candidates:
                            skill_id, cursor = probe, (cursor + step + 1) % len(order)
                            break
                    if skill_id is None:
                        skill_id = todays_skills[0] if todays_skills else order[0]
                    if skill_id not in todays_skills:
                        todays_skills.append(skill_id)
                    info = by_id[skill_id]
                    gain = round(info.gain * min(1.0, slot / info.minutes), 2)
                    remaining[skill_id] = max(0, remaining[skill_id] - slot)
                    items.append(PlannedItem(day, position, "practice", skill_id, slot, gain))
                    position += 1
                    budget -= slot
        day += timedelta(days=1)
        index += 1
    return items
