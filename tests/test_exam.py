from sqlalchemy import select

from mysite.db.models import AuditLog
from tests.conftest import bearer, register
from tests.factories import (
    ADMIN,
    FORMAT,
    SCALE,
    make_format,
    make_mini,
    make_mock,
    make_question,
    make_skill,
    make_staff,
    option_id,
    time_travel,
    transition,
)

API = "/api/v1"


async def _start(client, headers, test_id, **extra_headers):
    return await client.post(f"{API}/tests/{test_id}/start", headers={**headers, **extra_headers})


async def _answer(client, headers, session_id, question, key, seq=1, **extra):
    payload = {"option_id": option_id(question, key) if key else None, "seq": seq, **extra}
    return await client.put(
        f"{API}/sessions/{session_id}/answers/{question['id']}", json=payload, headers=headers
    )


async def _student(client, email="student@example.com") -> dict:
    return bearer(await register(client, email))


# ---------- formats, scales, test building ----------


async def test_only_admin_manages_formats_and_scales(client):
    staff = await make_staff(client)
    assert (await client.post(f"{ADMIN}/exam-formats", json=FORMAT, headers=staff.editor)).status_code == 403
    fv = await make_format(client, staff, with_scale=False)
    denied = await client.post(
        f"{ADMIN}/exam-format-versions/{fv['id']}/scales", json=SCALE, headers=staff.editor
    )
    assert denied.status_code == 403


async def test_scale_is_validated(client):
    staff = await make_staff(client)
    fv = await make_format(client, staff, with_scale=False)
    url = f"{ADMIN}/exam-format-versions/{fv['id']}/scales"

    def scale(points):
        return {
            "kind": "estimated",
            "points": [{"section_code": c, "raw": r, "scaled": s} for c, r, s in points],
        }

    math_ok = [("math", 0, 0), ("math", 3, 60)]
    bad = {
        "missing section": math_ok,
        "wrong max raw": [*math_ok, ("reading", 0, 0), ("reading", 5, 40)],
        "decreasing": [*math_ok, ("reading", 0, 40), ("reading", 2, 10)],
        "unknown section": [*math_ok, ("history", 0, 0), ("history", 2, 10)],
    }
    for name, points in bad.items():
        response = await client.post(url, json=scale(points), headers=staff.admin)
        assert response.status_code == 422, name
        assert response.json()["error"]["code"] == "invalid_scale", name


async def test_new_scale_version_replaces_published_one_with_audit(client, db):
    staff = await make_staff(client)
    fv = await make_format(client, staff)  # scale v1 published
    second = await client.post(
        f"{ADMIN}/exam-format-versions/{fv['id']}/scales",
        json={**SCALE, "kind": "official"},
        headers=staff.admin,
    )
    assert second.json()["version"] == 2
    published = await client.post(f"{ADMIN}/score-scales/{second.json()['id']}/publish", headers=staff.admin)
    assert (published.json()["status"], published.json()["kind"]) == ("published", "official")
    again = await client.post(f"{ADMIN}/score-scales/{second.json()['id']}/publish", headers=staff.admin)
    assert again.status_code == 409

    actions = (await db.scalars(select(AuditLog.action).where(AuditLog.object_type == "score_scale"))).all()
    assert sorted(actions) == ["score_scale.create"] * 2 + ["score_scale.publish"] * 2


async def test_mock_must_match_the_format_to_be_published(client):
    staff = await make_staff(client)
    skill = await make_skill(client, staff)
    fv = await make_format(client, staff)
    published = [await make_question(client, staff, skill["id"]) for _ in range(4)]
    draft_question = (
        await client.post(
            f"{ADMIN}/questions",
            json={**(await _payload(skill))},
            headers=staff.editor,
        )
    ).json()

    body = {
        "code": "mock-bad",
        "title_ky": "Сыноо",
        "title_ru": "Пробный",
        "kind": "mock",
        "language": "ru",  # questions are in ky
        "format_version_id": fv["id"],
        "questions": [
            {"question_version_id": published[0]["id"], "section_code": "reading"},  # wrong order
            {"question_version_id": published[1]["id"], "section_code": "math"},
            {"question_version_id": published[2]["id"], "section_code": "math", "points": 2},
            {"question_version_id": published[3]["id"]},  # no section
            {"question_version_id": draft_question["id"], "section_code": "math"},  # not published
        ],
    }
    created = await client.post(f"{ADMIN}/tests", json=body, headers=staff.editor)
    assert created.status_code == 201, created.text
    test_id = created.json()["id"]

    report = (await client.get(f"{ADMIN}/tests/{test_id}/validate", headers=staff.editor)).json()
    assert report["valid"] is False
    text = " | ".join(report["problems"])
    for expected in (
        "not published",
        "differs from the test",
        "have no section",
        "Section reading: 1 questions, the format requires 2",
        "1 point each",
        "grouped by section",
    ):
        assert expected in text, expected

    publish = await client.post(f"{ADMIN}/tests/{test_id}/publish", headers=staff.admin)
    assert publish.status_code == 422
    assert publish.json()["error"]["code"] == "test_incomplete"

    # Editors build, but only admins publish.
    assert (await client.post(f"{ADMIN}/tests/{test_id}/publish", headers=staff.editor)).status_code == 403
    # Drafts are invisible to students.
    assert (await client.get(f"{API}/tests")).json() == []
    assert (await client.get(f"{API}/tests/{test_id}")).status_code == 404
    assert (await _start(client, await _student(client), test_id)).status_code == 404


async def _payload(skill) -> dict:
    from tests.factories import question_payload

    return question_payload(skill["id"])


async def test_published_test_is_frozen_and_listed(client):
    staff = await make_staff(client)
    mock = await make_mock(client, staff)

    catalog = (await client.get(f"{API}/tests", params={"kind": "mock"})).json()
    assert len(catalog) == 1
    item = catalog[0]
    assert (item["question_count"], item["time_limit_sec"], item["scale_kind"]) == (5, 900, "estimated")
    assert [(s["code"], s["question_count"], s["time_limit_sec"]) for s in item["sections"]] == [
        ("math", 3, 600),
        ("reading", 2, 300),
    ]
    assert (await client.get(f"{API}/tests", params={"kind": "mini"})).json() == []

    body = {
        "code": mock["code"], "title_ky": "x", "title_ru": "x", "kind": "mock", "language": "ky",
        "format_version_id": mock["format_version"]["id"], "questions": [],
    }  # fmt: skip
    frozen = await client.put(f"{ADMIN}/tests/{mock['id']}", json=body, headers=staff.editor)
    assert frozen.status_code == 409


# ---------- starting ----------


async def test_start_hides_answers_and_is_idempotent(client):
    staff = await make_staff(client)
    mock = await make_mock(client, staff)
    student = await _student(client)

    first = await _start(client, student, mock["id"])
    assert first.status_code == 201, first.text
    session = first.json()

    # Nothing in the payload reveals the answer key.
    assert "is_correct" not in first.text
    assert "solution" not in first.text
    assert "correct" not in first.text

    assert session["status"] == "in_progress"
    assert session["current_section_code"] == "math"
    # Only the open section is delivered: reading cannot be previewed.
    assert [q["section_code"] for q in session["questions"]] == ["math"] * 3
    assert [q["position"] for q in session["questions"]] == [1, 2, 3]
    assert [o["key"] for o in session["questions"][0]["options"]] == ["A", "B", "C", "D"]

    # A repeated start returns the same session instead of creating another.
    second = await _start(client, student, mock["id"])
    assert second.status_code == 200
    assert second.json()["id"] == session["id"]
    sessions = (await client.get(f"{API}/sessions", headers=student)).json()
    assert len(sessions) == 1

    assert (await client.post(f"{API}/tests/{mock['id']}/start")).status_code == 401


async def test_idempotency_key_returns_the_same_session_even_after_it_finished(client):
    staff = await make_staff(client)
    mini = await make_mini(client, staff, (await make_skill(client, staff))["id"])
    student = await _student(client)

    first = await _start(client, student, mini["id"], **{"Idempotency-Key": "key-1"})
    assert first.status_code == 201
    sid = first.json()["id"]
    await client.post(f"{API}/sessions/{sid}/submit", headers=student)

    # A late network retry of the original request must not open a new attempt.
    retry = await _start(client, student, mini["id"], **{"Idempotency-Key": "key-1"})
    assert retry.status_code == 200
    assert (retry.json()["id"], retry.json()["status"]) == (sid, "finished")

    # A new key (a deliberate new attempt) does.
    fresh = await _start(client, student, mini["id"], **{"Idempotency-Key": "key-2"})
    assert fresh.status_code == 201
    assert fresh.json()["id"] != sid


async def test_test_with_retired_question_cannot_be_started(client):
    staff = await make_staff(client)
    mock = await make_mock(client, staff)
    await transition(client, staff.admin, mock["questions"][0]["question_version_id"], "retired")

    response = await _start(client, await _student(client), mock["id"])
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "test_unavailable"


# ---------- answering ----------


async def test_autosave_rules(client):
    staff = await make_staff(client)
    mock = await make_mock(client, staff)
    student = await _student(client)
    session = (await _start(client, student, mock["id"])).json()
    sid = session["id"]
    q1, q2 = session["questions"][0], session["questions"][1]

    saved = await _answer(client, student, sid, q1, "A", seq=1, marked=True, time_spent_sec=20)
    assert saved.status_code == 200
    assert (saved.json()["applied"], saved.json()["option_id"], saved.json()["marked"]) == (
        True,
        option_id(q1, "A"),
        True,
    )

    # Changing the answer is allowed until submit.
    changed = await _answer(client, student, sid, q1, "B", seq=2)
    assert changed.json()["option_id"] == option_id(q1, "B")

    # A stale request (offline queue replay, late duplicate) is ignored: server state wins.
    stale = await _answer(client, student, sid, q1, "D", seq=1)
    assert stale.status_code == 200
    assert (stale.json()["applied"], stale.json()["option_id"], stale.json()["seq"]) == (
        False,
        option_id(q1, "B"),
        2,
    )
    duplicate = await _answer(client, student, sid, q1, "B", seq=2)
    assert duplicate.json()["applied"] is False

    # An option of another question is rejected.
    foreign = await client.put(
        f"{API}/sessions/{sid}/answers/{q1['id']}",
        json={"option_id": option_id(q2, "A"), "seq": 3},
        headers=student,
    )
    assert foreign.status_code == 422
    assert foreign.json()["error"]["code"] == "invalid_option"

    # Clearing an answer.
    cleared = await _answer(client, student, sid, q1, None, seq=3)
    assert cleared.json()["option_id"] is None

    # A reload restores exactly what the server confirmed.
    restored = (await client.get(f"{API}/sessions/{sid}", headers=student)).json()
    by_id = {q["id"]: q for q in restored["questions"]}
    assert (by_id[q1["id"]]["selected_option_id"], by_id[q1["id"]]["marked"], by_id[q1["id"]]["seq"]) == (
        None,
        True,
        3,
    )


async def test_other_users_session_is_not_found(client):
    staff = await make_staff(client)
    mock = await make_mock(client, staff)
    owner = await _student(client, "owner@example.com")
    intruder = await _student(client, "intruder@example.com")
    session = (await _start(client, owner, mock["id"])).json()
    sid, q1 = session["id"], session["questions"][0]

    assert (await client.get(f"{API}/sessions/{sid}", headers=intruder)).status_code == 404
    assert (await _answer(client, intruder, sid, q1, "B")).status_code == 404
    assert (await client.post(f"{API}/sessions/{sid}/submit", headers=intruder)).status_code == 404
    assert (await client.get(f"{API}/sessions/{sid}/result", headers=intruder)).status_code == 404
    assert (
        await client.post(f"{API}/sessions/{sid}/sections/math/close", headers=intruder)
    ).status_code == 404
    # Staff roles give no access to a student's session either.
    assert (await client.get(f"{API}/sessions/{sid}", headers=staff.admin)).status_code == 404

    # The intruder changed nothing.
    state = (await client.get(f"{API}/sessions/{sid}", headers=owner)).json()
    assert state["status"] == "in_progress"
    assert all(q["selected_option_id"] is None for q in state["questions"])


# ---------- full mock exam ----------


async def test_full_mock_exam_scoring_and_checkpoints(client):
    staff = await make_staff(client)
    mock = await make_mock(client, staff)
    student = await _student(client)
    session = (await _start(client, student, mock["id"])).json()
    sid = session["id"]
    math = session["questions"]

    # The result is hidden while the attempt is running.
    early = await client.get(f"{API}/sessions/{sid}/result", headers=student)
    assert early.status_code == 409
    assert early.json()["error"]["code"] == "session_in_progress"

    # Math: two right (B), one wrong.
    await _answer(client, student, sid, math[0], "B", time_spent_sec=40)
    await _answer(client, student, sid, math[1], "B")
    await _answer(client, student, sid, math[2], "A")

    # Reading is not open yet, so the math section must be closed first.
    not_open = await client.post(f"{API}/sessions/{sid}/sections/reading/close", headers=student)
    assert not_open.status_code == 409

    state = (await client.post(f"{API}/sessions/{sid}/sections/math/close", headers=student)).json()
    assert state["current_section_code"] == "reading"
    reading = state["questions"]
    assert [q["section_code"] for q in reading] == ["reading"] * 2
    # Closing again is a harmless no-op.
    assert (
        await client.post(f"{API}/sessions/{sid}/sections/math/close", headers=student)
    ).status_code == 200

    # A closed section cannot be changed any more.
    late = await _answer(client, student, sid, math[2], "B", seq=5)
    assert late.status_code == 409
    assert late.json()["error"]["code"] == "section_closed"

    # Reading: one right, one skipped.
    await _answer(client, student, sid, reading[0], "B")

    submitted = await client.post(f"{API}/sessions/{sid}/submit", headers=student)
    assert submitted.status_code == 200, submitted.text
    result = submitted.json()

    assert (result["raw_score"], result["max_score"]) == (3, 5)
    assert (result["correct"], result["wrong"], result["skipped"]) == (2 + 1, 1, 1)
    # math 2/3 -> 40.0, reading 1/2 -> 20.0 on the test scale
    assert [(s["code"], s["raw_score"], s["max_score"], s["scaled_score"]) for s in result["sections"]] == [
        ("math", 2, 3, 40.0),
        ("reading", 1, 2, 20.0),
    ]
    assert result["scaled_score"] == 60.0
    assert (result["scale_kind"], result["score_is_estimated"]) == ("estimated", True)
    assert result["finish_reason"] == "submitted"

    # Now, and only now, the key and the solutions are shown.
    by_position = {q["position"]: q for q in result["questions"]}
    wrong = by_position[3]
    assert wrong["is_correct"] is False
    assert wrong["selected_option_id"] == option_id(wrong, "A")
    assert wrong["correct_option_id"] == option_id(wrong, "B")
    assert wrong["solution"]
    assert wrong["skill_id"] == mock["skill"]["id"]
    assert by_position[5]["is_correct"] is None
    assert by_position[1]["time_spent_sec"] == 40

    assert result["checkpoint"]["kind"] == "baseline"
    assert result["checkpoint"]["total_scaled"] == 60.0

    # Submit is idempotent, and the stored result equals the submit response.
    assert (await client.post(f"{API}/sessions/{sid}/submit", headers=student)).json() == result
    assert (await client.get(f"{API}/sessions/{sid}/result", headers=student)).json() == result

    # After finishing nothing can be changed, and the state no longer carries questions.
    after = await _answer(client, student, sid, reading[1], "B", seq=9)
    assert after.status_code == 409
    assert after.json()["error"]["code"] == "session_finished"
    final_state = (await client.get(f"{API}/sessions/{sid}", headers=student)).json()
    assert (final_state["status"], final_state["questions"]) == ("finished", [])

    # A second attempt becomes a control checkpoint; the baseline stays what it was.
    second = (await _start(client, student, mock["id"])).json()
    assert second["id"] != sid
    for question in second["questions"]:
        await _answer(client, student, second["id"], question, "B")
    await client.post(f"{API}/sessions/{second['id']}/submit", headers=student)

    checkpoints = (await client.get(f"{API}/checkpoints", headers=student)).json()
    assert [(c["kind"], c["total_scaled"]) for c in checkpoints] == [("baseline", 60.0), ("control", 60.0)]
    assert checkpoints[1]["sections"] == [
        {"code": "math", "raw": 3, "max": 3, "scaled": 60.0},
        {"code": "reading", "raw": 0, "max": 2, "scaled": 0.0},
    ]


async def test_mock_without_published_scale_gives_raw_score_only(client):
    staff = await make_staff(client)
    mock = await make_mock(client, staff, with_scale=False)
    student = await _student(client)
    session = (await _start(client, student, mock["id"])).json()
    await _answer(client, student, session["id"], session["questions"][0], "B")

    result = (await client.post(f"{API}/sessions/{session['id']}/submit", headers=student)).json()
    assert (result["raw_score"], result["scaled_score"], result["scale_kind"]) == (1, None, None)
    assert result["score_is_estimated"] is False
    assert result["checkpoint"]["total_scaled"] is None


# ---------- time ----------


async def test_section_deadline_moves_to_next_section_on_time(client):
    staff = await make_staff(client)
    mock = await make_mock(client, staff)
    student = await _student(client)
    session = (await _start(client, student, mock["id"])).json()
    sid, math = session["id"], session["questions"]
    await _answer(client, student, sid, math[0], "B")

    # 700 s later: math (600 s) is over, reading (300 s) has been running for 100 s.
    await time_travel(700)

    late = await _answer(client, student, sid, math[1], "B")
    assert late.status_code == 409
    assert late.json()["error"]["code"] == "section_closed"

    state = (await client.get(f"{API}/sessions/{sid}", headers=student)).json()
    assert (state["status"], state["current_section_code"]) == ("in_progress", "reading")
    sections = {s["code"]: s for s in state["sections"]}
    # Math closed exactly at its deadline, and reading started at that same moment,
    # not when the student happened to come back.
    assert sections["math"]["closed_at"] == sections["math"]["deadline_at"]
    assert sections["reading"]["started_at"] == sections["math"]["deadline_at"]
    assert state["deadline_at"] == sections["reading"]["deadline_at"]

    assert (await _answer(client, student, sid, state["questions"][0], "B")).status_code == 200


async def test_expired_session_is_finalized_from_saved_answers(client):
    staff = await make_staff(client)
    mock = await make_mock(client, staff)
    student = await _student(client)
    session = (await _start(client, student, mock["id"])).json()
    sid = session["id"]
    await _answer(client, student, sid, session["questions"][0], "B")

    await time_travel(2000)  # everything (900 s) ran out while the student was away

    listed = (await client.get(f"{API}/sessions", headers=student)).json()
    assert (listed[0]["status"], listed[0]["raw_score"], listed[0]["scaled_score"]) == ("finished", 1, 20.0)

    result = (await client.get(f"{API}/sessions/{sid}/result", headers=student)).json()
    assert (result["finish_reason"], result["raw_score"]) == ("deadline", 1)
    assert result["duration_sec"] == 900  # ended at the deadline, not when it was noticed
    assert result["checkpoint"]["kind"] == "baseline"

    # A submit arriving after the deadline changes nothing.
    assert (await client.post(f"{API}/sessions/{sid}/submit", headers=student)).json() == result
    # And the test can be taken again as a new attempt.
    assert (await _start(client, student, mock["id"])).status_code == 201


async def test_expire_overdue_job(client, db):
    from mysite.services import sessions as session_service

    staff = await make_staff(client)
    mock = await make_mock(client, staff)
    student = await _student(client)
    await _start(client, student, mock["id"])
    assert await session_service.expire_overdue(db) == 0

    await time_travel(2000)
    assert await session_service.expire_overdue(db) == 1
    assert await session_service.expire_overdue(db) == 0
    assert len((await client.get(f"{API}/checkpoints", headers=student)).json()) == 1


async def test_plain_test_without_sections(client):
    staff = await make_staff(client)
    skill = await make_skill(client, staff)
    mini = await make_mini(client, staff, skill["id"])
    student = await _student(client)

    session = (await _start(client, student, mini["id"])).json()
    sid = session["id"]
    assert (session["current_section_code"], session["sections"], len(session["questions"])) == (None, [], 2)
    await _answer(client, student, sid, session["questions"][0], "B")
    assert (
        await client.post(f"{API}/sessions/{sid}/sections/math/close", headers=student)
    ).status_code == 404

    await time_travel(601)
    late = await _answer(client, student, sid, session["questions"][1], "B")
    assert late.status_code == 409
    assert late.json()["error"]["code"] == "session_finished"

    result = (await client.get(f"{API}/sessions/{sid}/result", headers=student)).json()
    assert (result["finish_reason"], result["raw_score"], result["max_score"]) == ("deadline", 1, 2)
    assert (result["scaled_score"], result["sections"], result["checkpoint"]) == (None, [], None)
    # Only mock exams are points on the score trajectory.
    assert (await client.get(f"{API}/checkpoints", headers=student)).json() == []


# ---------- concurrency ----------


async def test_parallel_requests_do_not_duplicate_anything(client, db):
    import asyncio

    from mysite.db.models import Checkpoint, TestSession

    staff = await make_staff(client)
    mock = await make_mock(client, staff)
    student = await _student(client)

    # A double tap on "Start": five requests at once still give one session.
    starts = await asyncio.gather(*[_start(client, student, mock["id"]) for _ in range(5)])
    assert sorted(r.status_code for r in starts) == [200, 200, 200, 200, 201]
    assert len({r.json()["id"] for r in starts}) == 1
    session = starts[0].json()
    sid = session["id"]

    # Autosave burst on one question: the highest seq wins whatever the arrival order.
    question = session["questions"][0]
    keys = ["A", "C", "D", "B"]
    await asyncio.gather(
        *[_answer(client, student, sid, question, key, seq=i + 1) for i, key in enumerate(keys)]
    )
    state = (await client.get(f"{API}/sessions/{sid}", headers=student)).json()
    assert state["questions"][0]["selected_option_id"] == option_id(question, "B")

    # Submit pressed several times at once: one result, one checkpoint.
    submits = await asyncio.gather(
        *[client.post(f"{API}/sessions/{sid}/submit", headers=student) for _ in range(5)]
    )
    assert all(r.status_code == 200 for r in submits)
    assert all(r.json() == submits[0].json() for r in submits)
    assert submits[0].json()["raw_score"] == 1

    assert len((await db.scalars(select(TestSession))).all()) == 1
    assert len((await db.scalars(select(Checkpoint))).all()) == 1
