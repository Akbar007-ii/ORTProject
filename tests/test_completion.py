"""Account export/delete, email, import, media, universities, strategy, reminders, security headers."""

import json
from datetime import UTC, date, datetime, timedelta

from sqlalchemy import func, select, text

from mysite.config import get_settings
from mysite.core import mailer
from mysite.db.database import SessionLocal, engine
from mysite.db.models import Attempt, AuditLog, NotificationLog, NotificationPreference, User
from mysite.services import notifications
from mysite.services import telegram as tg
from tests.conftest import PASSWORD, bearer, register
from tests.factories import ADMIN, make_skill, make_staff
from tests.test_learning import _goal, make_course, take_mock

API = "/api/v1"
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64


# ---------- account ----------


async def test_export_contains_the_students_data_only(client):
    staff = await make_staff(client)
    course = await make_course(client, staff)
    student = bearer(await register(client, "student@example.com"))
    await take_mock(client, student, course)
    await client.put(f"{API}/goals/current", json=_goal(target=80), headers=student)

    response = await client.get(f"{API}/account/export", headers=student)
    assert response.status_code == 200
    assert response.headers["content-disposition"].startswith("attachment;")
    assert response.headers["cache-control"] == "no-store"
    data = json.loads(response.content)
    assert data["profile"]["email"] == "student@example.com"
    assert {c["type"] for c in data["consents"]} == {"terms", "privacy"}
    assert len(data["answers"]) == 4
    assert len(data["mistake_cards"]) == 2
    assert data["checkpoints"][0]["kind"] == "baseline"
    assert data["goals"][0]["target_score"] == 80.0
    assert "password_hash" not in response.text
    assert "editor@example.com" not in response.text  # nobody else's data

    assert (await client.get(f"{API}/account/export")).status_code == 401


async def test_delete_account_erases_everything(client, db):
    staff = await make_staff(client)
    course = await make_course(client, staff)
    auth = await register(client, "student@example.com")
    student = bearer(auth)
    await take_mock(client, student, course)

    wrong = await client.request(
        "DELETE", f"{API}/account", json={"password": "wrong-password", "confirm": "DELETE"}, headers=student
    )
    assert wrong.status_code == 401
    unconfirmed = await client.request(
        "DELETE", f"{API}/account", json={"password": PASSWORD, "confirm": "yes"}, headers=student
    )
    assert unconfirmed.status_code == 400

    sent = []

    async def fake_send(to, subject, body):
        sent.append((to, subject))
        return True

    original = mailer.send
    mailer.send = fake_send
    try:
        deleted = await client.request(
            "DELETE", f"{API}/account", json={"password": PASSWORD, "confirm": "DELETE"}, headers=student
        )
    finally:
        mailer.send = original
    assert deleted.status_code == 204
    assert sent == [("student@example.com", "ORTO: аккаунт өчүрүлдү")]

    assert (await client.get(f"{API}/me", headers=student)).status_code == 401
    login = await client.post(
        f"{API}/auth/login", json={"email": "student@example.com", "password": PASSWORD}
    )
    assert login.status_code == 401
    assert (
        await db.scalar(select(func.count()).select_from(User).where(User.email == "student@example.com"))
        == 0
    )
    assert await db.scalar(select(func.count()).select_from(Attempt)) == 0
    # The audit row remains, without personal data.
    log = await db.scalar(select(AuditLog).where(AuditLog.action == "account.delete"))
    assert log.object_id == auth["user"]["id"] and log.actor_id is None
    # The email can be registered again.
    assert (await register(client, "student@example.com"))["user"]["id"] != auth["user"]["id"]


async def test_admin_cannot_delete_own_account(client):
    staff = await make_staff(client)
    response = await client.request(
        "DELETE", f"{API}/account", json={"password": PASSWORD, "confirm": "DELETE"}, headers=staff.admin
    )
    assert (response.status_code, response.json()["error"]["code"]) == (422, "admin_cannot_delete")


# ---------- email ----------


async def test_password_reset_email_goes_through_smtp(client, monkeypatch):
    await register(client, "student@example.com")
    messages = []
    monkeypatch.setattr(get_settings(), "smtp_host", "smtp.example.com")
    monkeypatch.setattr(mailer, "_send_sync", messages.append)

    await client.post(f"{API}/auth/password-reset/request", json={"email": "student@example.com"})
    assert len(messages) == 1
    message = messages[0]
    assert message["To"] == "student@example.com"
    assert message["Subject"] == "ORTO: сырсөздү калыбына келтирүү"
    body = message.get_content()
    token = body.split("token=")[1].split()[0]
    assert body.startswith("Сырсөздү") and "http://localhost:3000/reset-password?token=" in body

    confirmed = await client.post(
        f"{API}/auth/password-reset/confirm", json={"token": token, "new_password": "brand-new-password-42"}
    )
    assert confirmed.status_code == 204


async def test_smtp_failure_does_not_break_the_request(client, monkeypatch):
    await register(client, "student@example.com")
    monkeypatch.setattr(get_settings(), "smtp_host", "smtp.example.com")

    def broken(message):
        raise OSError("connection refused")

    monkeypatch.setattr(mailer, "_send_sync", broken)
    response = await client.post(f"{API}/auth/password-reset/request", json={"email": "student@example.com"})
    assert response.status_code == 202


# ---------- import ----------

CSV_HEADER = "external_id,skill_code,language,stem,option_a,option_b,option_c,option_d,correct,solution,difficulty,source,license\n"


def _csv(*rows: str) -> bytes:
    return (CSV_HEADER + "\n".join(rows) + "\n").encode()


async def _import(client, headers, content: bytes, name: str, dry_run: bool):
    return await client.post(
        f"{ADMIN}/questions/import",
        params={"dry_run": str(dry_run).lower()},
        files={"file": (name, content, "text/csv")},
        headers=headers,
    )


async def test_import_csv_dry_run_then_real_then_idempotent(client):
    staff = await make_staff(client)
    await make_skill(client, staff, "math.quadratic")
    good = _csv(
        'q1,math.quadratic,ky,"$2+2$?",3,4,5,6,B,2+2=4,1,ORTO,own',
        'q1,math.quadratic,ru,"$2+2$?",3,4,5,6,B,2+2=4,1,ORTO,own',
        'q2,math.quadratic,ky,"$3 \\cdot 3$?",6,8,9,12,C,3·3=9,2,ORTO,own',
    )

    dry = (await _import(client, staff.editor, good, "bank.csv", True)).json()
    assert (dry["ok"], dry["total"], dry["created"], dry["skipped"], dry["errors"]) == (True, 3, 3, 0, [])
    listing = (await client.get(f"{ADMIN}/questions", headers=staff.editor)).json()
    assert listing["total"] == 0  # a dry run writes nothing

    real = (await _import(client, staff.editor, good, "bank.csv", False)).json()
    assert (real["ok"], real["created"]) == (True, 3)
    items = (await client.get(f"{ADMIN}/questions", headers=staff.editor)).json()["items"]
    assert len(items) == 3 and all(i["status"] == "draft" for i in items)  # still need review
    by_lang = {(i["language"], i["stem"]) for i in items}
    assert ("ru", "$2+2$?") in by_lang
    ky_and_ru = [i for i in items if i["stem"] == "$2+2$?"]
    assert ky_and_ru[0]["group_id"] == ky_and_ru[1]["group_id"]  # one task, two languages
    assert [o["key"] for o in items[0]["options"] if o["is_correct"]] in (["B"], ["C"])

    again = (await _import(client, staff.editor, good, "bank.csv", False)).json()
    assert (again["created"], again["skipped"]) == (0, 3)
    assert (await client.get(f"{ADMIN}/questions", headers=staff.editor)).json()["total"] == 3


async def test_import_reports_bad_rows_and_imports_nothing(client):
    staff = await make_staff(client)
    await make_skill(client, staff, "math.quadratic")
    bad = _csv(
        "ok1,math.quadratic,ky,Суроо,1,2,3,4,A,чечим,3,ORTO,own",
        "bad1,math.unknown,ky,Суроо,1,2,3,4,A,чечим,3,ORTO,own",
        "bad2,math.quadratic,ky,Суроо,1,2,3,,A,чечим,3,ORTO,own",
        "bad3,math.quadratic,ky,Суроо,1,2,3,4,E,чечим,3,ORTO,own",
        "bad4,math.quadratic,ky,Суроо,1,2,3,4,A,,3,ORTO,own",
        "bad5,math.quadratic,de,Суроо,1,2,3,4,A,чечим,3,ORTO,own",
        "bad6,math.quadratic,ky,Суроо,1,2,3,4,A,чечим,9,ORTO,own",
        ",math.quadratic,ky,Суроо,1,2,3,4,A,чечим,3,ORTO,own",
    )
    report = (await _import(client, staff.editor, bad, "bank.csv", False)).json()
    assert report["ok"] is False and report["created"] == 0
    errors = {e["row"]: e["error"] for e in report["errors"]}
    assert set(errors) == {3, 4, 5, 6, 7, 8, 9}  # CSV row numbers, the header is row 1
    assert "unknown skill_code" in errors[3]
    assert "4 options" in errors[4]
    assert "correct must name" in errors[5]
    assert "solution is required" in errors[6]
    assert "language" in errors[7]
    assert "difficulty" in errors[8]
    assert "external_id" in errors[9]
    assert (await client.get(f"{ADMIN}/questions", headers=staff.editor)).json()["total"] == 0


async def test_import_json_and_access(client):
    staff = await make_staff(client)
    await make_skill(client, staff, "math.quadratic")
    payload = json.dumps(
        {
            "questions": [
                {
                    "external_id": "j1",
                    "skill_code": "math.quadratic",
                    "language": "ru",
                    "stem": "Сколько будет 5+5?",
                    "options": {"A": "10", "B": "11", "C": "12", "D": "55"},
                    "correct": "A",
                    "solution": "5+5=10",
                    "source": "ORTO",
                    "license": "own",
                }
            ]
        },
        ensure_ascii=False,
    ).encode()
    report = (await _import(client, staff.editor, payload, "bank.json", False)).json()
    assert (report["ok"], report["created"]) == (True, 1)

    for name, content, code in (
        ("bank.txt", b"x", "invalid_file"),
        ("bank.json", b"{not json", "invalid_file"),
        ("bank.csv", CSV_HEADER.encode(), "invalid_file"),
    ):
        response = await _import(client, staff.editor, content, name, True)
        assert (response.status_code, response.json()["error"]["code"]) == (422, code)

    student = bearer(await register(client, "student@example.com"))
    assert (await _import(client, student, payload, "bank.json", True)).status_code == 403


# ---------- media ----------


async def test_media_upload_and_download(client):
    staff = await make_staff(client)

    async def upload(content, headers=staff.editor, name="pic.png"):
        return await client.post(
            f"{ADMIN}/media", files={"file": (name, content, "image/png")}, headers=headers
        )

    created = await upload(PNG)
    assert created.status_code == 201, created.text
    body = created.json()
    assert body["content_type"] == "image/png" and body["markdown"] == f"![]({body['url']})"
    # Same picture again: the same file, not a copy.
    assert (await upload(PNG)).json()["id"] == body["id"]

    served = await client.get(body["url"])  # public: students load it inside questions
    assert served.status_code == 200
    assert served.content == PNG and served.headers["content-type"] == "image/png"
    assert served.headers["x-content-type-options"] == "nosniff"

    # The type comes from the bytes: an SVG (scripts) or a renamed text file is refused.
    svg = await upload(b"<svg onload='alert(1)'></svg>", name="pic.png")
    assert (svg.status_code, svg.json()["error"]["code"]) == (422, "unsupported_file")
    big = await upload(PNG + b"\x00" * (2 * 1024 * 1024))
    assert big.status_code == 413
    student = bearer(await register(client, "student@example.com"))
    assert (await upload(PNG, headers=student)).status_code == 403
    assert (await client.get(f"{API}/media/00000000-0000-4000-8000-000000000000")).status_code == 404


# ---------- universities ----------


async def test_university_threshold_becomes_the_goal(client):
    staff = await make_staff(client)
    course = await make_course(client, staff)
    student = bearer(await register(client, "student@example.com"))

    uni = await client.put(
        f"{ADMIN}/universities",
        json={"code": "knu", "title_ky": "КУУ", "title_ru": "КНУ", "city": "Бишкек"},
        headers=staff.admin,
    )
    assert uni.status_code == 200
    assert (
        await client.put(
            f"{ADMIN}/universities",
            json={"code": "x", "title_ky": "x", "title_ru": "x"},
            headers=staff.editor,
        )
    ).status_code == 403
    program_body = {
        "code": "cs", "title_ky": "Информатика", "title_ru": "Информатика", "year": 2026,
        "threshold_score": 75, "source": "тест маалыматы",
    }  # fmt: skip
    program = await client.post(
        f"{ADMIN}/universities/{uni.json()['id']}/programs", json=program_body, headers=staff.admin
    )
    assert program.status_code == 201
    duplicate = await client.post(
        f"{ADMIN}/universities/{uni.json()['id']}/programs", json=program_body, headers=staff.admin
    )
    assert duplicate.status_code == 409
    no_source = await client.post(
        f"{ADMIN}/universities/{uni.json()['id']}/programs",
        json={**program_body, "year": 2025, "source": ""},
        headers=staff.admin,
    )
    assert no_source.status_code == 400

    listing = (await client.get(f"{API}/universities/programs", params={"q": "информ"})).json()
    assert [(p["university_ru"], p["threshold_score"], p["source"]) for p in listing] == [
        ("КНУ", 75.0, "тест маалыматы")
    ]
    assert (await client.get(f"{API}/universities/programs", params={"max_score": 50})).json() == []

    await take_mock(client, student, course)  # 40 points
    goal = await client.put(
        f"{API}/goals/current",
        json={
            "program_id": program.json()["id"],
            "exam_date": str(date.today() + timedelta(days=30)),
            "minutes_per_day": 60,
        },
        headers=student,
    )
    assert goal.status_code == 200, goal.text
    body = goal.json()
    assert body["target_score"] == 75.0
    assert (body["program"]["university_ky"], body["program"]["year"]) == ("КУУ", 2026)
    neither = await client.put(
        f"{API}/goals/current",
        json={"exam_date": str(date.today() + timedelta(days=30)), "minutes_per_day": 60},
        headers=student,
    )
    assert neither.status_code == 400


# ---------- strategy ----------


async def test_strategy_advice_after_a_mock(client):
    staff = await make_staff(client)
    course = await make_course(client, staff)
    student = bearer(await register(client, "student@example.com"))
    session = (await client.post(f"{API}/tests/{course['test_id']}/start", headers=student)).json()
    sid = session["id"]
    q1, q2, q3 = session["questions"]

    async def answer(question, key, seconds):
        await client.put(
            f"{API}/sessions/{sid}/answers/{question['id']}",
            json={
                "option_id": next(o["id"] for o in question["options"] if o["key"] == key),
                "seq": 1,
                "time_spent_sec": seconds,
            },
            headers=student,
        )

    await answer(q1, "B", 400)  # far over the 90 s norm
    await answer(q2, "A", 5)  # wrong in 5 seconds
    early = await client.get(f"{API}/sessions/{sid}/strategy", headers=student)
    assert early.status_code == 409

    async with engine.begin() as conn:  # the math section runs out of time with q3 unanswered
        await conn.execute(
            text(
                "UPDATE test_sessions SET started_at = started_at - interval '20 minutes', deadline_at = deadline_at - interval '20 minutes'"
            )
        )
        await conn.execute(
            text(
                "UPDATE session_sections SET started_at = started_at - interval '20 minutes', "
                "deadline_at = deadline_at - interval '20 minutes'"
            )
        )
    await client.post(f"{API}/sessions/{sid}/submit", headers=student)

    advice = (await client.get(f"{API}/sessions/{sid}/strategy", headers=student)).json()
    assert advice["slow_questions"] == [1]
    assert advice["rushed_questions"] == [2]
    assert advice["unanswered_questions"] == [3, 4, 5]
    codes = [t["code"] for t in advice["tips"]]
    assert "ran_out" in codes and "too_slow" in codes and "rushed" in codes
    assert "guess_blanks" not in codes  # the format does not say wrong answers are free
    math = advice["sections"][0]
    assert (math["ran_out_of_time"], math["unanswered"], math["time_used_sec"]) == (True, 1, 600)
    assert all(t["text"] for t in advice["tips"])

    other = bearer(await register(client, "other@example.com"))
    assert (await client.get(f"{API}/sessions/{sid}/strategy", headers=other)).status_code == 404


async def test_strategy_tells_apart_finishing_early_with_blanks(client):
    staff = await make_staff(client)
    course = await make_course(client, staff)
    student = bearer(await register(client, "student@example.com"))
    session = (await client.post(f"{API}/tests/{course['test_id']}/start", headers=student)).json()
    first = session["questions"][0]
    await client.put(
        f"{API}/sessions/{session['id']}/answers/{first['id']}",
        json={
            "option_id": next(o["id"] for o in first["options"] if o["key"] == "B"),
            "seq": 1,
            "time_spent_sec": 30,
        },
        headers=student,
    )
    await client.post(f"{API}/sessions/{session['id']}/submit", headers=student)  # right away, with blanks

    tips = (await client.get(f"{API}/sessions/{session['id']}/strategy", headers=student)).json()["tips"]
    assert [(t["code"], t["count"]) for t in tips] == [("left_early", 2), ("left_early", 2)]
    assert "бош калтырдыңыз" in tips[0]["text"]


# ---------- reminders ----------


async def _linked_student(client, email="student@example.com", telegram_id=111):
    auth = await register(client, email)
    student = bearer(auth)
    token = (await client.post(f"{API}/telegram/link-token", headers=student)).json()["token"]
    async with SessionLocal() as db:
        assert await tg.link(db, telegram_id, token) == "linked"
    return student


def test_quiet_hours():
    assert notifications.in_quiet_hours(23, 22, 8) and notifications.in_quiet_hours(3, 22, 8)
    assert not notifications.in_quiet_hours(12, 22, 8)
    assert notifications.in_quiet_hours(14, 13, 15) and not notifications.in_quiet_hours(15, 13, 15)
    assert not notifications.in_quiet_hours(5, 7, 7)


async def test_reminders_respect_consent_time_and_send_once(client):
    student = await _linked_student(client)
    sent: list[tuple[int, str]] = []

    async def send(chat_id, text_):
        sent.append((chat_id, text_))

    # 19:00 in Bishkek (UTC+6) on a Monday.
    evening = datetime(2026, 11, 16, 13, 0, tzinfo=UTC)
    async with SessionLocal() as db:
        assert await notifications.run_once(db, send, evening) == {"sent": 0, "failed": 0, "blocked": 0}
    assert sent == []  # off until the student turns it on

    prefs = await client.patch(
        f"{API}/me/notifications", json={"telegram_enabled": True, "reminder_hour": 18}, headers=student
    )
    assert prefs.json() | {} == {
        "telegram_enabled": True, "telegram_linked": True, "reminder_hour": 18,
        "quiet_start": 22, "quiet_end": 8, "weekly_report": True,
    }  # fmt: skip
    assert (
        await client.patch(f"{API}/me/notifications", json={"reminder_hour": 25}, headers=student)
    ).status_code == 400

    morning = datetime(2026, 11, 16, 4, 0, tzinfo=UTC)  # 10:00 local, before the reminder hour
    night = datetime(2026, 11, 16, 17, 30, tzinfo=UTC)  # 23:30 local, quiet hours
    async with SessionLocal() as db:
        for moment in (morning, night):
            assert (await notifications.run_once(db, send, moment))["sent"] == 0
        assert (await notifications.run_once(db, send, evening))["sent"] == 1
        # Running again the same day (or a second worker) sends nothing more.
        assert (await notifications.run_once(db, send, evening + timedelta(minutes=30)))["sent"] == 0
    assert len(sent) == 1 and sent[0][0] == 111
    assert "Бүгүнкү окуу" in sent[0][1] and "Максат коюла элек" in sent[0][1]

    # Sunday evening: the daily reminder plus the weekly report.
    sunday = datetime(2026, 11, 22, 13, 0, tzinfo=UTC)
    async with SessionLocal() as db:
        assert (await notifications.run_once(db, send, sunday))["sent"] == 2
    assert "Жуманын жыйынтыгы" in sent[-1][1]


async def test_failed_sends_are_retried_and_blocked_bots_are_switched_off(client):
    student = await _linked_student(client)
    await client.patch(f"{API}/me/notifications", json={"telegram_enabled": True}, headers=student)
    evening = datetime(2026, 11, 16, 13, 0, tzinfo=UTC)
    calls = []

    async def flaky(chat_id, text_):
        calls.append(chat_id)
        raise RuntimeError("telegram is down")

    async with SessionLocal() as db:
        for _ in range(5):
            await notifications.run_once(db, flaky, evening)
        log = await db.scalar(select(NotificationLog))
    assert (len(calls), log.status, log.attempts) == (3, "failed", 3)  # gives up after 3 tries

    async def blocked(chat_id, text_):
        raise notifications.BotBlocked()

    async with SessionLocal() as db:
        counts = await notifications.run_once(db, blocked, evening + timedelta(days=1))
        prefs = await db.get(
            NotificationPreference,
            (await db.scalar(select(User.id).where(User.email == "student@example.com"))),
        )
    assert counts["blocked"] == 1
    assert prefs.telegram_enabled is False


# ---------- security headers ----------


async def test_security_headers(client):
    response = await client.get("/health")
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["x-frame-options"] == "DENY"
    assert response.headers["content-security-policy"].startswith("default-src 'none'")
    assert "content-security-policy" not in (await client.get("/docs")).headers
