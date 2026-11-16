"""AI tutor (with a fake model) and Telegram linking / bot replies (without Telegram)."""

from sqlalchemy import func, select, text

from mysite.config import get_settings
from mysite.db.database import SessionLocal, engine
from mysite.db.models import AiUsageLog, TutorExplanation
from mysite.services import telegram as tg
from mysite.services import tutor as tutor_service
from tests.conftest import bearer, register
from tests.factories import ADMIN, make_staff
from tests.test_learning import _goal, days_pass, make_course, take_mock

API = "/api/v1"


class FakeLLM:
    def __init__(self, text="Түшүндүрмө: D = b² − 4ac.", fail=False):
        self.text, self.fail, self.calls = text, fail, []

    async def explain(self, system: str, user: str) -> tutor_service.LLMResult:
        self.calls.append((system, user))
        if self.fail:
            raise RuntimeError("provider is down")
        return tutor_service.LLMResult(self.text, "fake-model", 120, 40)


async def _answered(client):
    staff = await make_staff(client)
    course = await make_course(client, staff)
    student = bearer(await register(client, "student@example.com"))
    result = await take_mock(client, student, course)  # question 3 answered wrongly (A)
    wrong = next(q for q in result["questions"] if q["position"] == 3)
    versions = (await client.get(f"{ADMIN}/questions", params={"limit": 100}, headers=staff.editor)).json()
    version_id = next(v["id"] for v in versions["items"] if v["code"] == wrong["question_code"])
    return staff, course, student, version_id


async def _count(model) -> int:
    async with SessionLocal() as db:
        return await db.scalar(select(func.count()).select_from(model))


async def _explain(client, student, version_id, mode="short"):
    return await client.post(
        f"{API}/tutor/explain", json={"question_version_id": version_id, "mode": mode}, headers=student
    )


# ---------- tutor ----------


async def test_tutor_without_api_key_returns_the_verified_solution(client, monkeypatch):
    staff, course, student, version_id = await _answered(client)
    monkeypatch.setattr(tutor_service, "get_llm", lambda: None)

    response = await _explain(client, student, version_id)
    assert response.status_code == 200, response.text
    body = response.json()
    assert (body["source"], body["cached"], body["mode"]) == ("verified_solution", False, "short")
    assert "D = b²" in body["content"]
    assert await _count(AiUsageLog) == 0


async def test_tutor_explains_from_verified_context_and_caches(client, monkeypatch):
    staff, course, student, version_id = await _answered(client)
    llm = FakeLLM()
    monkeypatch.setattr(tutor_service, "get_llm", lambda: llm)

    first = (await _explain(client, student, version_id, "detailed")).json()
    assert (first["source"], first["cached"], first["content"]) == ("ai", False, llm.text)
    assert first["remaining_today"] == 29

    # The model saw the verified key and solution, the student's wrong option, and the language.
    system, prompt = llm.calls[0]
    assert "Correct option: B" in prompt and "Verified solution:" in prompt
    assert "The student chose option A." in prompt
    assert "Kyrgyz" in system and "every step" in system
    assert "student@example.com" not in system + prompt  # no personal data goes to the provider

    # Same question, mode and language: served from the cache, the model is not called again.
    second = (await _explain(client, student, version_id, "detailed")).json()
    assert (second["source"], second["cached"], second["content"]) == ("ai", True, llm.text)
    assert len(llm.calls) == 1
    # Another mode is another explanation.
    await _explain(client, student, version_id, "example")
    assert len(llm.calls) == 2
    assert await _count(TutorExplanation) == 2
    assert await _count(AiUsageLog) == 2


async def test_tutor_falls_back_when_the_model_fails_or_answers_badly(client, monkeypatch):
    staff, course, student, version_id = await _answered(client)

    for llm in (FakeLLM(fail=True), FakeLLM(text=None), FakeLLM(text="x" * 7000)):
        monkeypatch.setattr(tutor_service, "get_llm", lambda llm=llm: llm)
        body = (await _explain(client, student, version_id)).json()
        assert (body["source"], body["cached"]) == ("verified_solution", False)
        assert "D = b²" in body["content"]
    assert await _count(TutorExplanation) == 0  # nothing bad is cached
    async with SessionLocal() as db:
        statuses = (await db.scalars(select(AiUsageLog.status).order_by(AiUsageLog.created_at))).all()
    assert statuses == ["error", "rejected", "rejected"]


async def test_tutor_daily_quota(client, monkeypatch):
    staff, course, student, version_id = await _answered(client)
    llm = FakeLLM()
    monkeypatch.setattr(tutor_service, "get_llm", lambda: llm)
    monkeypatch.setattr(get_settings(), "tutor_daily_limit", 1)

    one = (await _explain(client, student, version_id)).json()
    assert (one["source"], one["remaining_today"]) == ("ai", 0)
    # Over the limit: still an answer (the verified solution), but no new model call.
    two = (await _explain(client, student, version_id, "detailed")).json()
    assert (two["source"], two["remaining_today"]) == ("verified_solution", 0)
    assert len(llm.calls) == 1


async def test_tutor_never_reveals_unanswered_or_withdrawn_questions(client, monkeypatch):
    staff, course, student, version_id = await _answered(client)
    llm = FakeLLM()
    monkeypatch.setattr(tutor_service, "get_llm", lambda: llm)

    # A practice question the student has not answered: asking for its explanation gives nothing.
    assert (await _explain(client, student, course["extra"]["alg"][0])).status_code == 404
    # Another student did not answer this one either.
    other = bearer(await register(client, "other@example.com"))
    assert (await _explain(client, other, version_id)).status_code == 404
    assert (
        await client.post(f"{API}/tutor/explain", json={"question_version_id": version_id})
    ).status_code == 401

    # Withdrawn content is not explained with confidence.
    await client.post(
        f"{ADMIN}/questions/versions/{version_id}/transition", json={"to": "retired"}, headers=staff.admin
    )
    withdrawn = await _explain(client, student, version_id)
    assert (withdrawn.status_code, withdrawn.json()["error"]["code"]) == (409, "no_verified_solution")
    assert llm.calls == []


async def test_question_reports_reach_the_content_team(client):
    staff, course, student, version_id = await _answered(client)
    created = await client.post(
        f"{API}/question-reports",
        json={"question_version_id": version_id, "kind": "explanation", "comment": "Түшүнүксүз"},
        headers=student,
    )
    assert created.status_code == 201
    unseen = await client.post(
        f"{API}/question-reports",
        json={"question_version_id": course["extra"]["alg"][0], "kind": "question"},
        headers=student,
    )
    assert unseen.status_code == 404

    assert (await client.get(f"{ADMIN}/question-reports", headers=student)).status_code == 403
    queue = (await client.get(f"{ADMIN}/question-reports", headers=staff.expert)).json()
    assert [(r["kind"], r["comment"]) for r in queue] == [("explanation", "Түшүнүксүз")]
    resolved = await client.post(f"{ADMIN}/question-reports/{queue[0]['id']}/resolve", headers=staff.editor)
    assert resolved.json()["status"] == "resolved"
    assert (await client.get(f"{ADMIN}/question-reports", headers=staff.expert)).json() == []


# ---------- telegram ----------


async def test_telegram_link_token_is_one_time_and_short_lived(client, db):
    student = bearer(await register(client, "student@example.com"))
    assert (await client.post(f"{API}/telegram/link-token")).status_code == 401

    old = (await client.post(f"{API}/telegram/link-token", headers=student)).json()
    token = (await client.post(f"{API}/telegram/link-token", headers=student)).json()
    assert (token["expires_in"], token["deep_link"]) == (600, None)

    assert await tg.link(db, 111, "wrong-token") == "bad_token"
    assert await tg.link(db, 111, old["token"]) == "bad_token"  # replaced by the newer one
    assert await tg.link(db, 111, token["token"]) == "linked"
    assert await tg.link(db, 222, token["token"]) == "bad_token"  # single use
    assert (await tg.user_for(db, 111)).email == "student@example.com"
    assert await tg.user_for(db, 222) is None

    # A Telegram account that is already linked cannot be taken over by another user's token.
    other = bearer(await register(client, "other@example.com"))
    other_token = (await client.post(f"{API}/telegram/link-token", headers=other)).json()["token"]
    assert await tg.link(db, 111, other_token) == "taken"
    assert (await tg.user_for(db, 111)).email == "student@example.com"

    # An expired token does not work.
    fresh = (await client.post(f"{API}/telegram/link-token", headers=other)).json()["token"]
    async with engine.begin() as conn:
        await conn.execute(text("UPDATE telegram_link_tokens SET expires_at = now() - interval '1 hour'"))
    assert await tg.link(db, 333, fresh) == "bad_token"

    assert (await client.delete(f"{API}/telegram/link", headers=student)).status_code == 204
    async with SessionLocal() as session:
        assert await tg.user_for(session, 111) is None


async def test_bot_replies_and_review_share_one_history_with_the_site(client):
    staff = await make_staff(client)
    course = await make_course(client, staff)
    student = bearer(await register(client, "student@example.com"))
    token = (await client.post(f"{API}/telegram/link-token", headers=student)).json()["token"]
    async with SessionLocal() as db:
        assert await tg.link(db, 111, token) == "linked"

    async def as_bot(action, *args):
        async with SessionLocal() as db:
            return await action(db, await tg.user_for(db, 111), *args)

    assert "азырынча маалымат жок" in await as_bot(tg.stats_text)
    assert "Максат коюла элек" in await as_bot(tg.goal_text)
    assert (await as_bot(tg.next_card)).buttons == []

    await take_mock(client, student, course)
    assert "Чечилди: 4" in await as_bot(tg.stats_text)
    assert "1 күн" in await as_bot(tg.streak_text)
    assert "geo" in await as_bot(tg.weak_text)

    await days_pass(1)
    card = await as_bot(tg.next_card)
    assert [label for label, _ in card.buttons] == ["A", "B", "C", "D"]
    assert all(len(data.encode()) <= 64 for _, data in card.buttons)  # Telegram's callback limit
    correct = next(data for label, data in card.buttons if label == "B")

    # Someone else's Telegram account pressing a copied button changes nothing.
    intruder = bearer(await register(client, "intruder@example.com"))
    intruder_token = (await client.post(f"{API}/telegram/link-token", headers=intruder)).json()["token"]
    async with SessionLocal() as db:
        await tg.link(db, 999, intruder_token)
        assert "кайталоодо эмес" in await tg.answer_card(db, await tg.user_for(db, 999), correct)

    assert "Туура!" in await as_bot(tg.answer_card, correct)
    # The same button pressed again: the card is no longer due.
    assert "кайталоодо эмес" in await as_bot(tg.answer_card, correct)
    assert "кайталоодо эмес" in await as_bot(tg.answer_card, "r:garbage")

    # The website sees the review done in Telegram.
    learning_cards = await client.get(f"{API}/mistakes", params={"status": "learning"}, headers=student)
    assert learning_cards.json()["total"] == 1
    assert (await client.get(f"{API}/review/queue", headers=student)).json()["due"] == 1

    # A wrong answer in the chat shows the right option.
    second = await as_bot(tg.next_card)
    wrong = next(data for label, data in second.buttons if label == "C")
    assert "Туура жооп: B" in await as_bot(tg.answer_card, wrong)

    # Goal and plan in the chat, then the language switch.
    await client.put(f"{API}/goals/current", json=_goal(target=80), headers=student)
    assert "Максат: 80" in await as_bot(tg.goal_text)
    today = await as_bot(tg.today_text)
    assert "Бүгүнкү план (0/4)" in today and "Практика · alg" in today
    assert "русский" in await as_bot(tg.set_language, "ru")
    assert "Цель: 80" in await as_bot(tg.goal_text)
    assert (await client.get(f"{API}/me", headers=student)).json()["locale"] == "ru"
