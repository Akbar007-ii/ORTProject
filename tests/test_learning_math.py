"""Hand-checked numbers for the pure learning maths."""

import uuid
from datetime import UTC, date, datetime, timedelta

from mysite.services import mastery, planning

NOW = datetime(2026, 11, 13, 9, 0, tzinfo=UTC)
TODAY = date(2026, 11, 13)
A, B = uuid.UUID(int=1), uuid.UUID(int=2)


def test_mastery_level():
    assert mastery.mastery(0, 0) == 0.5  # no data: the prior
    assert mastery.mastery(0, 0, prior=0.2) == 0.2
    assert mastery.mastery(10, 10) == 0.917  # (10 + 0.5*2) / 12
    assert mastery.mastery(0, 10) == 0.083
    assert mastery.is_mastered(6, 6)
    assert not mastery.is_mastered(5, 6)  # 83% < 85%
    assert not mastery.is_mastered(4, 4)  # too few answers to tell


def test_review_schedule_reaches_mastery_after_three_days_of_success():
    first = mastery.after_success(0, 0, None, NOW, TODAY)
    assert (first.interval_days, first.consecutive_successes, first.mastered) == (3, 1, False)
    assert first.due_at == NOW + timedelta(days=3)

    second = mastery.after_success(first.interval_index, 1, TODAY, NOW, TODAY + timedelta(days=3))
    assert (second.interval_days, second.consecutive_successes, second.mastered) == (7, 2, False)

    third = mastery.after_success(second.interval_index, 2, TODAY, NOW, TODAY + timedelta(days=10))
    assert (third.mastered, third.due_at, third.interval_days) == (True, None, 0)


def test_two_successes_on_one_day_count_once():
    again = mastery.after_success(1, 1, TODAY, NOW, TODAY)
    assert again.consecutive_successes == 1
    assert not again.mastered


def test_failure_returns_to_first_interval():
    state = mastery.after_failure(TODAY, NOW)
    assert (state.interval_index, state.consecutive_successes, state.interval_days) == (0, 0, 1)
    assert state.due_at == NOW + timedelta(days=1)


def test_streaks():
    days = {TODAY - timedelta(days=i) for i in range(3)}
    assert mastery.current_streak(days, TODAY) == 3
    # Nothing done yet today: yesterday's streak is still alive.
    assert mastery.current_streak(days - {TODAY}, TODAY) == 2
    assert mastery.current_streak({TODAY - timedelta(days=2)}, TODAY) == 0
    assert mastery.current_streak(set(), TODAY) == 0
    assert mastery.longest_streak(days | {TODAY - timedelta(days=10)}) == 3


def test_scale_helpers():
    table = [(0, 0.0), (3, 60.0)]
    assert planning.point_value(table) == 20.0
    assert planning.scale_value(table, 2.5) == 50.0
    assert planning.scale_value(table, 9) == 60.0


def _skill(skill_id, mastery_level, questions=3.0, value=20.0):
    return planning.SkillInput(skill_id, "math", questions, mastery_level, value)


def test_skill_roi():
    item = planning.skill_roi(_skill(A, 0.35))
    # gap 0.5 * 3 questions * 20 points = 30 points;
    # 10 answers * 1.5 min * 1.5 (difficulty 3) * 2 (study overhead) = 45 minutes
    assert (item.gain, item.minutes, item.roi) == (30.0, 45, 40.0)
    done = planning.skill_roi(_skill(A, 0.9))
    assert (done.gain, done.minutes) == (0.0, 0)


def test_rank_orders_by_points_per_hour_and_drops_finished_skills():
    heavy = _skill(A, 0.35, questions=6)  # same time, twice the points
    light = _skill(B, 0.35, questions=3)
    finished = _skill(uuid.UUID(int=3), 0.95)
    assert [i.skill_id for i in planning.rank([light, finished, heavy])] == [A, B]


def test_effort_to_close():
    ranked = planning.rank([_skill(A, 0.35)])
    assert planning.effort_to_close(15, ranked) == (23, 30.0)  # half of the skill: 22.5 -> 23 min
    assert planning.effort_to_close(100, ranked) == (45, 30.0)  # cannot gain more than 30
    assert planning.effort_to_close(0, ranked) == (0, 30.0)


def test_expected_raw():
    section = planning.SectionInput("math", 3, [(0, 0), (3, 60)], prior=0.5, skills=[(2, 1.0)])
    assert planning.expected_raw(section) == 2.5  # 2 known questions + 1 unknown at the prior
    overweight = planning.SectionInput("math", 3, [(0, 0), (3, 60)], prior=0.5, skills=[(6, 1.0)])
    assert planning.expected_raw(overweight) == 3.0  # weights are scaled down to the section size


def test_forecast_range_and_confidence():
    sections = [
        planning.SectionInput("math", 3, [(0, 0), (3, 60)], prior=0.5, skills=[(2, 1.0)]),  # 2.5 -> 50
        planning.SectionInput("reading", 2, [(0, 0), (2, 40)], prior=0.5),  # 1.0 -> 20
    ]
    fresh = planning.forecast(sections, checkpoint_age_days=0, answers_since=0)
    assert (fresh.low, fresh.point, fresh.high, fresh.confidence) == (65.0, 70.0, 75.0, "medium")

    stale = planning.forecast(sections, checkpoint_age_days=40, answers_since=0)
    assert (stale.low, stale.high, stale.confidence) == (58.0, 82.0, "low")  # +-12% of 100

    active = planning.forecast(sections, checkpoint_age_days=5, answers_since=60)
    assert (active.low, active.high, active.confidence) == (67.0, 73.0, "high")


def test_milestones():
    assert planning.milestones(90, 200) == [112.0, 134.0, 156.0, 178.0, 200.0]
    assert planning.milestones(60, 70) == [70.0]


def test_track():
    start, exam = date(2026, 11, 1), date(2026, 12, 11)  # 40 days
    args = (60, 100, start, exam, date(2026, 11, 11))  # day 10 -> expected 70
    assert planning.track(*args, 75) == ("ahead", 70.0)
    assert planning.track(*args, 70) == ("on_track", 70.0)
    assert planning.track(*args, 60) == ("behind", 70.0)
    assert planning.track(*args, None) == ("unknown", 70.0)


def test_build_plan():
    ranked = planning.rank([_skill(A, 0.35), _skill(B, 0.35, questions=1.5)])
    exam = TODAY + timedelta(days=10)
    items = planning.build_plan(TODAY, exam, 60, ranked, mock_minutes=15)

    days = sorted({i.day for i in items})
    assert days == [TODAY + timedelta(days=n) for n in range(10)]  # nothing on the exam day

    by_day = {day: [i for i in items if i.day == day] for day in days}
    mock_day = exam - timedelta(days=2)
    assert [(i.kind, i.minutes) for i in by_day[mock_day]] == [("mock", 15)]  # rehearsal before the exam

    first = by_day[TODAY]
    assert [(i.kind, i.minutes) for i in first] == [
        ("review", 10),
        ("practice", 15),
        ("practice", 15),
        ("practice", 20),
    ]
    assert first[1].skill_id == A  # the best ROI comes first
    assert {i.skill_id for i in first[1:]} == {A, B}
    for day, day_items in by_day.items():
        if day != mock_day:
            assert sum(i.minutes for i in day_items) == 60
    assert all(i.expected_gain > 0 for i in items if i.kind == "practice")


def test_build_plan_respects_time_already_spent_today():
    ranked = planning.rank([_skill(A, 0.35)])
    items = planning.build_plan(TODAY, TODAY + timedelta(days=5), 60, ranked, 15, used_today=50)
    assert sum(i.minutes for i in items if i.day == TODAY) <= 10


def test_skill_in_two_sections_is_learned_once_and_counted_in_both():
    in_math = planning.SkillInput(A, "math", 3.0, 0.35, 20.0)  # 30 points, 45 min
    in_reading = planning.SkillInput(A, "reading", 1.0, 0.35, 10.0)  # 5 points, 45 min
    (merged,) = planning.rank([in_math, in_reading])
    assert (merged.gain, merged.minutes, merged.section_code) == (35.0, 45, "math")
    assert merged.roi == round(35 / 0.75, 2)
