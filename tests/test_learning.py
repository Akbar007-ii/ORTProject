"""The learning loop through the API: mock exam -> mistakes -> review -> goal -> plan."""

from datetime import date, timedelta

from sqlalchemy import text

from mysite.db.database import engine
from tests.conftest import bearer, register, register_payload
from tests.factories import ADMIN, make_format, make_question, make_staff, option_id

API = "/api/v1"


async def make_course(client, staff) -> dict:
    """Two skills. The mock exam has 3 algebra questions (math) and 2 geometry ones (reading
    section, to keep the test format); each skill also has 4 extra questions for practice."""
    subject = (
        await client.post(
            f"{ADMIN}/subjects",
            json={"code": "math", "title_ky": "Математика", "title_ru": "Математика"},
            headers=staff.editor,
        )
    ).json()
    topic = (
        await client.post(
            f"{ADMIN}/topics",
            json={"subject_id": subject["id"], "title_ky": "Тема", "title_ru": "Тема"},
            headers=staff.editor,
        )
    ).json()
    skills = {}
    for code in ("alg", "geo"):
        skills[code] = (
            await client.post(
                f"{ADMIN}/skills",
                json={"topic_id": topic["id"], "code": f"math.{code}", "title_ky": code, "title_ru": code},
                headers=staff.editor,
            )
        ).json()
    fv = await make_format(client, staff)

    mock_questions, extra = [], {"alg": [], "geo": []}
    for code, section, count in (("alg", "math", 3), ("geo", "reading", 2)):
        for _ in range(count):
            version = await make_question(client, staff, skills[code]["id"])
            mock_questions.append({"question_version_id": version["id"], "section_code": section})
        for _ in range(4):
            extra[code].append((await make_question(client, staff, skills[code]["id"]))["id"])
    created = await client.post(
        f"{ADMIN}/tests",
        json={
            "code": "mock-1",
            "title_ky": "Сыноо",
            "title_ru": "Пробный",
            "kind": "mock",
            "language": "ky",
            "format_version_id": fv["id"],
            "questions": mock_questions,
        },  # fmt: skip
        headers=staff.editor,
    )
    assert created.status_code == 201, created.text
    published = await client.post(f"{ADMIN}/tests/{created.json()['id']}/publish", headers=staff.admin)
    assert published.status_code == 200, published.text
    return {
        "test_id": published.json()["id"],
        "skills": skills,
        "topic": topic,
        "format_version": fv,
        "extra": extra,
    }


async def take_mock(client, student, course, math=("B", "B", "A"), reading=("A", None)) -> dict:
    """Takes the mock exam. The correct option is always B; None skips a question."""
    session = (await client.post(f"{API}/tests/{course['test_id']}/start", headers=student)).json()
    sid = session["id"]

    async def answer_all(questions, keys):
        for question, key in zip(questions, keys, strict=True):
            if key:
                response = await client.put(
                    f"{API}/sessions/{sid}/answers/{question['id']}",
                    json={"option_id": option_id(question, key), "seq": 1, "time_spent_sec": 30},
                    headers=student,
                )
                assert response.status_code == 200, response.text

    await answer_all(session["questions"], math)
    state = (await client.post(f"{API}/sessions/{sid}/sections/math/close", headers=student)).json()
    await answer_all(state["questions"], reading)
    result = await client.post(f"{API}/sessions/{sid}/submit", headers=student)
    assert result.status_code == 200, result.text
    return result.json()


async def days_pass(days: int) -> None:
    """Moves every card `days` into the past, as if that much time had gone by."""
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "UPDATE mistake_cards SET due_at = due_at - make_interval(days => :d), "
                "last_success_on = last_success_on - CAST(:d AS integer)"
            ),
            {"d": days},
        )


async def _setup(client, email="student@example.com"):
    staff = await make_staff(client)
    course = await make_course(client, staff)
    student = bearer(await register(client, email))
    return staff, course, student


def _key(card_or_question: dict, key: str) -> str:
    return option_id(card_or_question, key)


# ---------- mistakes and review ----------


async def test_mock_mistakes_become_cards_and_review_leads_to_mastery(client):
    staff, course, student = await _setup(client)
    result = await take_mock(client, student, course)
    assert (result["raw_score"], result["scaled_score"]) == (2, 40.0)

    # Two wrong answers -> two cards. The skipped question is not a mistake.
    journal = (await client.get(f"{API}/mistakes", headers=student)).json()
    assert journal["total"] == 2
    card = next(c for c in journal["items"] if c["skill_id"] == course["skills"]["alg"]["id"])
    assert (card["status"], card["source"], card["error_count"]) == ("new", "test", 1)
    assert card["selected_option_id"] == _key(card, "A")
    assert card["correct_option_id"] == _key(card, "B")
    assert card["solution"]

    # Statistics come from the same history.
    by_code = {s["skill_code"]: s for s in (await client.get(f"{API}/stats/skills", headers=student)).json()}
    assert (by_code["math.alg"]["answers"], by_code["math.alg"]["accuracy"]) == (3, 0.667)
    assert (by_code["math.geo"]["answers"], by_code["math.geo"]["accuracy"]) == (1, 0.0)

    # Cards are due a day later, not immediately.
    assert (await client.get(f"{API}/review/queue", headers=student)).json()["due"] == 0
    early = await client.post(
        f"{API}/review/{card['id']}/answer", json={"option_id": _key(card, "B")}, headers=student
    )
    assert early.status_code == 409
    assert early.json()["error"]["code"] == "card_not_due"

    await days_pass(1)
    queue = await client.get(f"{API}/review/queue", headers=student)
    assert queue.json()["due"] == 2
    assert queue.json()["estimated_minutes"] == 3
    # A due card is a question again: no key, no solution.
    assert "solution" not in queue.text and "correct" not in queue.text

    # First success: next review in 3 days.
    headers = {**student, "Idempotency-Key": "review-1"}
    first = await client.post(
        f"{API}/review/{card['id']}/answer", json={"option_id": _key(card, "B")}, headers=headers
    )
    assert first.status_code == 200, first.text
    body = first.json()
    assert (body["is_correct"], body["status"], body["consecutive_successes"], body["interval_days"]) == (
        True, "learning", 1, 3,
    )  # fmt: skip
    assert body["solution"]

    # The same request again (lost response): same answer, the card does not move twice.
    replay = await client.post(
        f"{API}/review/{card['id']}/answer", json={"option_id": _key(card, "B")}, headers=headers
    )
    assert replay.json() == body
    assert (await client.get(f"{API}/stats/overview", headers=student)).json()[
        "solved"
    ] == 5  # 4 in the mock + 1

    # A second success on the same calendar day does not count towards mastery.
    async with engine.begin() as conn:
        await conn.execute(
            text("UPDATE mistake_cards SET due_at = now() - interval '1 minute' WHERE id = :id"),
            {"id": card["id"]},
        )
    same_day = (
        await client.post(
            f"{API}/review/{card['id']}/answer", json={"option_id": _key(card, "B")}, headers=student
        )
    ).json()
    assert (same_day["consecutive_successes"], same_day["status"]) == (1, "learning")

    # Two more successes on later days -> mastered and out of the schedule.
    await days_pass(8)
    second = (
        await client.post(
            f"{API}/review/{card['id']}/answer", json={"option_id": _key(card, "B")}, headers=student
        )
    ).json()
    assert (second["consecutive_successes"], second["status"]) == (2, "learning")
    await days_pass(31)
    third = (
        await client.post(
            f"{API}/review/{card['id']}/answer", json={"option_id": _key(card, "B")}, headers=student
        )
    ).json()
    assert (third["consecutive_successes"], third["status"], third["next_due_at"]) == (3, "mastered", None)

    mastered = (await client.get(f"{API}/mistakes", params={"status": "mastered"}, headers=student)).json()
    assert [c["id"] for c in mastered["items"]] == [card["id"]]
    codes = {a["code"] for a in (await client.get(f"{API}/achievements", headers=student)).json()}
    assert {"first_mock", "cards_mastered_1"} <= codes


async def test_review_failure_snooze_archive_and_reason(client):
    staff, course, student = await _setup(client)
    await take_mock(client, student, course)
    card = (await client.get(f"{API}/mistakes", headers=student)).json()["items"][0]
    url = f"{API}/review/{card['id']}"
    await days_pass(1)

    # A wrong answer in review: back to the 1-day interval, error counter grows.
    failed = (await client.post(f"{url}/answer", json={"option_id": _key(card, "C")}, headers=student)).json()
    assert (failed["is_correct"], failed["interval_days"], failed["consecutive_successes"]) == (False, 1, 0)
    refreshed = (await client.get(f"{API}/mistakes", headers=student)).json()
    assert next(c for c in refreshed["items"] if c["id"] == card["id"])["error_count"] == 2
    assert refreshed["total"] == 2  # still one card per question, no duplicate

    # An option of another question is rejected.
    await days_pass(1)
    other = next(c for c in refreshed["items"] if c["id"] != card["id"])
    foreign = await client.post(f"{url}/answer", json={"option_id": _key(other, "B")}, headers=student)
    assert foreign.status_code == 422

    # Snooze postpones the card by a day and is not a success.
    assert (await client.post(f"{url}/snooze", headers=student)).status_code == 200
    queue = (await client.get(f"{API}/review/queue", headers=student)).json()
    assert card["id"] not in [item["card_id"] for item in queue["items"]]

    # The student's own guess at the reason; it can be cleared again.
    patched = await client.patch(
        f"{API}/mistakes/{card['id']}", json={"reason": "calculation"}, headers=student
    )
    assert patched.json()["reason"] == "calculation"
    assert (
        await client.patch(f"{API}/mistakes/{card['id']}", json={"reason": "bad"}, headers=student)
    ).status_code == 400

    # Archived cards leave review; restoring makes them due at once.
    archived = await client.patch(f"{API}/mistakes/{card['id']}", json={"archived": True}, headers=student)
    assert archived.json()["status"] == "suspended"
    assert (
        await client.post(f"{url}/answer", json={"option_id": _key(card, "B")}, headers=student)
    ).status_code == 409
    restored = await client.patch(f"{API}/mistakes/{card['id']}", json={"archived": False}, headers=student)
    assert restored.json()["status"] == "learning"
    assert (
        await client.post(f"{url}/answer", json={"option_id": _key(card, "B")}, headers=student)
    ).status_code == 200

    # Nobody else can see or touch the card.
    intruder = bearer(await register(client, "intruder@example.com"))
    assert (await client.get(f"{API}/mistakes", headers=intruder)).json()["total"] == 0
    assert (
        await client.post(f"{url}/answer", json={"option_id": _key(card, "B")}, headers=intruder)
    ).status_code == 404
    assert (
        await client.patch(f"{API}/mistakes/{card['id']}", json={"archived": True}, headers=intruder)
    ).status_code == 404
    assert (await client.post(f"{url}/snooze", headers=intruder)).status_code == 404


async def test_withdrawn_question_leaves_the_review_queue(client):
    staff, course, student = await _setup(client)
    await take_mock(client, student, course)
    await days_pass(1)
    queue = (await client.get(f"{API}/review/queue", headers=student)).json()
    assert queue["due"] == 2

    journal = (await client.get(f"{API}/mistakes", headers=student)).json()["items"]
    code = journal[0]["question_code"]
    versions = (await client.get(f"{ADMIN}/questions", params={"limit": 100}, headers=staff.editor)).json()[
        "items"
    ]
    version_id = next(v["id"] for v in versions if v["code"] == code)
    retired = await client.post(
        f"{ADMIN}/questions/versions/{version_id}/transition", json={"to": "retired"}, headers=staff.admin
    )
    assert retired.status_code == 200

    assert (await client.get(f"{API}/review/queue", headers=student)).json()["due"] == 1
    # The history of the mistake itself is kept.
    assert (await client.get(f"{API}/mistakes", headers=student)).json()["total"] == 2


# ---------- practice ----------


async def test_practice_gives_feedback_once_and_prefers_new_questions(client):
    staff, course, student = await _setup(client)
    await take_mock(client, student, course)  # the three mock algebra questions are now "seen"
    alg = course["skills"]["alg"]["id"]

    started = await client.post(
        f"{API}/practice/sessions", json={"skill_id": alg, "count": 3}, headers=student
    )
    assert started.status_code == 201, started.text
    session = started.json()
    assert "solution" not in started.text and "correct_option_id" not in started.text
    assert (session["total"], session["answered"]) == (3, 0)
    assert {q["question_version_id"] for q in session["questions"]} <= set(course["extra"]["alg"])
    q1, q2, q3 = session["questions"]
    url = f"{API}/practice/sessions/{session['id']}"

    solved_before = (await client.get(f"{API}/stats/overview", headers=student)).json()["solved"]
    wrong = await client.post(
        f"{url}/answers/{q1['id']}", json={"option_id": _key(q1, "D"), "time_spent_sec": 50}, headers=student
    )
    assert wrong.status_code == 200, wrong.text
    feedback = wrong.json()
    assert (feedback["is_correct"], feedback["correct_option_id"]) == (False, _key(q1, "B"))
    assert feedback["solution"]

    # One answer per question: changing it after seeing the key is not possible.
    retry = await client.post(f"{url}/answers/{q1['id']}", json={"option_id": _key(q1, "B")}, headers=student)
    assert retry.json() == feedback
    assert (await client.get(f"{API}/stats/overview", headers=student)).json()["solved"] == solved_before + 1

    # The practice mistake joined the journal.
    practice_cards = (
        await client.get(f"{API}/mistakes", params={"source": "practice"}, headers=student)
    ).json()
    assert practice_cards["total"] == 1

    assert (
        await client.post(f"{url}/answers/{q2['id']}", json={"option_id": _key(q2, "B")}, headers=student)
    ).json()["is_correct"]
    foreign = await client.post(
        f"{url}/answers/{q3['id']}", json={"option_id": _key(q1, "B")}, headers=student
    )
    assert foreign.status_code == 422

    finished = (await client.post(f"{url}/finish", headers=student)).json()
    assert (finished["status"], finished["answered"], finished["correct"]) == ("finished", 2, 1)
    assert finished["questions"][0]["feedback"]["is_correct"] is False
    assert finished["questions"][2]["feedback"] is None  # unanswered: key still hidden
    late = await client.post(f"{url}/answers/{q3['id']}", json={"option_id": _key(q3, "B")}, headers=student)
    assert late.status_code == 409

    intruder = bearer(await register(client, "intruder@example.com"))
    assert (await client.get(url, headers=intruder)).status_code == 404
    assert (await client.post(f"{url}/finish", headers=intruder)).status_code == 404


async def test_practice_by_topic_and_language(client):
    staff, course, student = await _setup(client)
    by_topic = await client.post(
        f"{API}/practice/sessions", json={"topic_id": course["topic"]["id"], "count": 30}, headers=student
    )
    assert by_topic.status_code == 201
    skills = {q["skill_id"] for q in by_topic.json()["questions"]}
    assert skills == {course["skills"]["alg"]["id"], course["skills"]["geo"]["id"]}
    assert by_topic.json()["total"] == 13  # every published question of both skills

    # A student studying in Russian gets no Kyrgyz questions.
    russian = await client.post(f"{API}/auth/register", json=register_payload("ru@example.com", locale="ru"))
    empty = await client.post(
        f"{API}/practice/sessions",
        json={"skill_id": course["skills"]["alg"]["id"]},
        headers=bearer(russian.json()),
    )
    assert empty.status_code == 422
    assert empty.json()["error"]["code"] == "no_questions"

    both = await client.post(
        f"{API}/practice/sessions",
        json={"skill_id": course["skills"]["alg"]["id"], "topic_id": course["topic"]["id"]},
        headers=student,
    )
    assert both.status_code == 400


# ---------- goal, roi, forecast, plan ----------


def _goal(target=90, days=30, minutes=60) -> dict:
    return {
        "target_score": target,
        "exam_date": str(date.today() + timedelta(days=days)),
        "minutes_per_day": minutes,
    }


async def test_goal_needs_a_measured_starting_point(client):
    staff, course, student = await _setup(client)

    forecast = (await client.get(f"{API}/prediction", headers=student)).json()
    assert (forecast["available"], forecast["reason"]) == (False, "no_checkpoint")
    refused = await client.put(f"{API}/goals/current", json=_goal(), headers=student)
    assert refused.status_code == 422
    assert refused.json()["error"]["code"] == "baseline_required"
    assert (await client.get(f"{API}/goals/current", headers=student)).status_code == 404
    assert (await client.get(f"{API}/plan/today", headers=student)).status_code == 404

    # No data yet: null, not a misleading 0%.
    overview = (await client.get(f"{API}/stats/overview", headers=student)).json()
    assert (overview["solved"], overview["accuracy"], overview["streak"]) == (0, None, 0)

    # A mock exam taken elsewhere can be the starting point.
    future = await client.post(
        f"{API}/checkpoints/self-reported",
        json={"total_scaled": 55, "taken_on": str(date.today() + timedelta(days=3))},
        headers=student,
    )
    assert future.status_code == 422
    reported = await client.post(
        f"{API}/checkpoints/self-reported",
        json={"total_scaled": 55, "taken_on": str(date.today())},
        headers=student,
    )
    assert reported.status_code == 201
    assert reported.json()["kind"] == "self_reported"
    goal = await client.put(f"{API}/goals/current", json=_goal(target=80), headers=student)
    assert goal.status_code == 200, goal.text
    assert goal.json()["start_score"] == 55.0


async def test_goal_roi_forecast_and_progress(client):
    staff, course, student = await _setup(client)
    await take_mock(client, student, course)  # math 2/3 -> 40, reading 0/2 -> 0

    # Right after the mock exam the forecast equals the measured score.
    forecast = (await client.get(f"{API}/prediction", headers=student)).json()
    assert (forecast["available"], forecast["point"], forecast["low"], forecast["high"]) == (
        True,
        40.0,
        35.0,
        45.0,
    )
    assert (forecast["confidence"], forecast["is_estimated_scale"]) == ("medium", True)

    roi = (await client.get(f"{API}/skills/roi", headers=student)).json()
    assert [item["skill_code"] for item in roi] == ["math.alg", "math.geo"]
    alg, geo = roi
    # Algebra: 3 questions at 20 points, mastery 0.667 -> about 11 points to gain, quickly.
    assert (alg["expected_questions"], alg["mastery"], alg["potential_gain"], alg["minutes"]) == (
        3.0,
        0.667,
        10.98,
        18,
    )
    # Geometry: 2 questions, mastery 0 -> 34 points, but it takes much longer.
    assert (geo["expected_questions"], geo["mastery"], geo["potential_gain"], geo["minutes"]) == (
        2.0,
        0.0,
        34.0,
        77,
    )
    assert alg["roi"] > geo["roi"]

    for payload, code in (
        (_goal(target=101), "target_too_high"),
        (_goal(target=30), "target_too_low"),
        (_goal(days=0), "invalid_exam_date"),
    ):
        response = await client.put(f"{API}/goals/current", json=payload, headers=student)
        assert (response.status_code, response.json()["error"]["code"]) == (422, code)

    goal = (await client.put(f"{API}/goals/current", json=_goal(target=90), headers=student)).json()
    assert (goal["start_score"], goal["target_score"], goal["max_score"], goal["days_left"]) == (
        40.0,
        90.0,
        100.0,
        30,
    )
    assert [m["score"] for m in goal["milestones"]] == [65.0, 90.0]

    progress = (await client.get(f"{API}/goals/current/progress", headers=student)).json()
    assert (progress["current_score"], progress["gap"], progress["next_milestone"]) == (40.0, 50.0, 65.0)
    assert (progress["track"], progress["expected_score_today"]) == ("on_track", 40.0)
    # Honest: 40 + 45 reachable points is 85, below the target of 90.
    assert (progress["max_reachable"], progress["feasible"]) == (85.0, False)
    assert (progress["minutes_needed"], progress["minutes_available"]) == (95, 1800)

    reachable = (await client.put(f"{API}/goals/current", json=_goal(target=80), headers=student)).json()
    assert reachable["id"] != goal["id"]  # the old goal is archived, not overwritten
    assert (await client.get(f"{API}/goals/current/progress", headers=student)).json()["feasible"] is True


async def test_practice_moves_the_forecast_and_second_mock_reaches_milestones(client):
    staff, course, student = await _setup(client)
    await take_mock(client, student, course)
    await client.put(f"{API}/goals/current", json=_goal(target=90), headers=student)

    # Four correct geometry answers in practice: the forecast goes up before any new mock exam.
    session = (
        await client.post(
            f"{API}/practice/sessions",
            json={"skill_id": course["skills"]["geo"]["id"], "count": 4},
            headers=student,
        )
    ).json()
    for question in session["questions"]:
        await client.post(
            f"{API}/practice/sessions/{session['id']}/answers/{question['id']}",
            json={"option_id": _key(question, "B")},
            headers=student,
        )
    forecast = (await client.get(f"{API}/prediction", headers=student)).json()
    # geometry: 4 of the last 5 correct, prior 0 -> (4 + 0) / 7 = 0.571 -> 1.14 questions -> 23 points
    assert forecast["point"] == 63.0

    # But a milestone is earned only by a measured result.
    goal = (await client.get(f"{API}/goals/current", headers=student)).json()
    assert all(m["reached_at"] is None for m in goal["milestones"])

    result = await take_mock(client, student, course, math=("B", "B", "B"), reading=("B", "B"))
    assert (result["scaled_score"], result["checkpoint"]["kind"]) == (100.0, "control")

    goal = (await client.get(f"{API}/goals/current", headers=student)).json()
    assert all(m["reached_at"] for m in goal["milestones"])
    achievements = (await client.get(f"{API}/achievements", headers=student)).json()
    codes = [a["code"] for a in achievements]
    assert codes.count("milestone") == 2
    assert "goal_reached" in codes and "first_mock" in codes

    trajectory = (await client.get(f"{API}/stats/score-trajectory", headers=student)).json()
    assert [(p["kind"], p["total_scaled"]) for p in trajectory["points"]] == [
        ("baseline", 40.0),
        ("control", 100.0),
    ]
    assert trajectory["target_score"] == 90.0
    assert trajectory["prediction"]["available"] is True

    report = (await client.get(f"{API}/reports/weekly", headers=student)).json()
    assert report["solved"] == 13  # 4 + 4 + 5
    assert (report["streak"], report["active_days"]) == (1, 1)
    assert report["next_milestone"] is None


async def test_plan(client):
    staff, course, student = await _setup(client)
    await take_mock(client, student, course)
    await client.put(f"{API}/goals/current", json=_goal(target=80, days=20, minutes=60), headers=student)

    plan = (await client.get(f"{API}/plan", headers=student)).json()
    assert plan["version"] == 1
    assert len(plan["days"]) == 20  # up to the day before the exam
    today = plan["days"][0]
    assert today["day"] == str(date.today())
    assert [i["kind"] for i in today["items"]] == ["review", "practice", "practice", "practice"]
    assert today["minutes"] == 60
    first_practice = today["items"][1]
    assert first_practice["skill_id"] == course["skills"]["alg"]["id"]  # best points per hour first
    assert first_practice["expected_gain"] > 0
    kinds = [i["kind"] for day in plan["days"] for i in day["items"]]
    assert kinds.count("mock") == 2  # control mock after two weeks + rehearsal before the exam

    # Asking again does not build a new plan; an explicit rebuild does.
    assert (await client.get(f"{API}/plan", headers=student)).json()["id"] == plan["id"]

    # Doing a planned practice ticks the plan item.
    session = (
        await client.post(
            f"{API}/practice/sessions",
            json={"skill_id": first_practice["skill_id"], "count": 2, "plan_item_id": first_practice["id"]},
            headers=student,
        )
    ).json()
    await client.post(f"{API}/practice/sessions/{session['id']}/finish", headers=student)
    summary = (await client.get(f"{API}/plan/today", headers=student)).json()
    assert (summary["done"], summary["total"], summary["streak"]) == (1, 4, 1)

    skipped = await client.patch(
        f"{API}/plan/items/{today['items'][2]['id']}", json={"status": "skipped"}, headers=student
    )
    assert skipped.json()["status"] == "skipped"

    rebuilt = (await client.post(f"{API}/plan/rebuild", headers=student)).json()
    assert (rebuilt["version"], rebuilt["id"] != plan["id"]) == (2, True)
    # What was already done or skipped today is carried into the new plan.
    statuses = [i["status"] for i in rebuilt["days"][0]["items"]]
    assert statuses.count("done") == 1 and statuses.count("skipped") == 1

    intruder = bearer(await register(client, "intruder@example.com"))
    foreign = await client.patch(
        f"{API}/plan/items/{first_practice['id']}", json={"status": "done"}, headers=intruder
    )
    assert foreign.status_code == 404
    stolen = await client.post(
        f"{API}/practice/sessions",
        json={"skill_id": first_practice["skill_id"], "plan_item_id": first_practice["id"]},
        headers=intruder,
    )
    assert stolen.status_code == 422


async def test_expert_skill_weights_drive_roi(client):
    staff, course, student = await _setup(client)
    await take_mock(client, student, course)
    url = f"{ADMIN}/exam-format-versions/{course['format_version']['id']}/skill-weights"
    alg, geo = course["skills"]["alg"]["id"], course["skills"]["geo"]["id"]

    assert (await client.put(url, json=[], headers=student)).status_code == 403
    too_many = await client.put(
        url, json=[{"skill_id": alg, "section_code": "math", "expected_questions": 4}], headers=staff.admin
    )
    assert (too_many.status_code, too_many.json()["error"]["code"]) == (422, "invalid_weights")

    # Experts say geometry also appears in the math section.
    saved = await client.put(
        url,
        json=[
            {"skill_id": alg, "section_code": "math", "expected_questions": 1},
            {"skill_id": geo, "section_code": "math", "expected_questions": 2},
        ],
        headers=staff.admin,
    )
    assert saved.json() == {"rows": 2}
    roi = (await client.get(f"{API}/skills/roi", headers=student)).json()
    assert {(i["skill_code"], i["section_code"], i["expected_questions"]) for i in roi} == {
        ("math.alg", "math", 1.0),
        ("math.geo", "math", 2.0),
    }
