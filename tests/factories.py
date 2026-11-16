"""Builders that create content the same way staff would: through the API and its workflow."""

from dataclasses import dataclass

from httpx import AsyncClient
from sqlalchemy import text

from mysite.db.database import engine
from tests.conftest import bearer, register_with_role

ADMIN = "/api/v1/admin"
KEYS = "ABCD"


@dataclass
class Staff:
    admin: dict
    editor: dict
    expert: dict


async def make_staff(client: AsyncClient) -> Staff:
    return Staff(
        admin=bearer(await register_with_role(client, "admin@example.com", "admin")),
        editor=bearer(await register_with_role(client, "editor@example.com", "editor")),
        expert=bearer(await register_with_role(client, "expert@example.com", "expert")),
    )


async def make_skill(client: AsyncClient, staff: Staff, code: str = "math.quadratic") -> dict:
    subject = await client.post(
        f"{ADMIN}/subjects",
        json={"code": code.split(".")[0], "title_ky": "Математика", "title_ru": "Математика"},
        headers=staff.editor,
    )
    assert subject.status_code == 201, subject.text
    topic = await client.post(
        f"{ADMIN}/topics",
        json={"subject_id": subject.json()["id"], "title_ky": "Алгебра", "title_ru": "Алгебра"},
        headers=staff.editor,
    )
    skill = await client.post(
        f"{ADMIN}/skills",
        json={"topic_id": topic.json()["id"], "code": code, "title_ky": "Навык", "title_ru": "Навык"},
        headers=staff.editor,
    )
    assert skill.status_code == 201, skill.text
    return {**skill.json(), "subject_id": subject.json()["id"]}


def question_payload(skill_id: str, correct: str = "B", **overrides) -> dict:
    return {
        "skill_id": skill_id,
        "language": "ky",
        "stem": "Теңдемени чыгар: $x^2 - 5x + 6 = 0$",
        "options": [{"key": k, "text": f"Вариант {k}", "is_correct": k == correct} for k in KEYS],
        "solution": "1. D = b² − 4ac\n2. x = 2, x = 3",
        "difficulty": 3,
        "source": "ORTO авторлору",
        "license": "own",
        **overrides,
    }


async def transition(
    client: AsyncClient, headers: dict, version_id: str, to: str, comment: str | None = None
):
    return await client.post(
        f"{ADMIN}/questions/versions/{version_id}/transition",
        json={"to": to, "comment": comment},
        headers=headers,
    )


async def publish_question(client: AsyncClient, staff: Staff, version_id: str) -> dict:
    """Walks a draft through the whole review chain with the right role at each step."""
    steps = [
        (staff.editor, "editorial_review"),
        (staff.editor, "expert_review"),
        (staff.expert, "approved"),
        (staff.admin, "published"),
    ]
    response = None
    for headers, to in steps:
        response = await transition(client, headers, version_id, to)
        assert response.status_code == 200, response.text
    return response.json()


async def make_question(
    client: AsyncClient, staff: Staff, skill_id: str, correct: str = "B", **overrides
) -> dict:
    created = await client.post(
        f"{ADMIN}/questions", json=question_payload(skill_id, correct, **overrides), headers=staff.editor
    )
    assert created.status_code == 201, created.text
    return await publish_question(client, staff, created.json()["id"])


FORMAT = {
    "code": "ort_test",
    "title_ky": "Сыноо форматы",
    "title_ru": "Тестовый формат",
    "sections": [
        {
            "code": "math",
            "title_ky": "Математика",
            "title_ru": "Математика",
            "question_count": 3,
            "time_limit_sec": 600,
        },
        {
            "code": "reading",
            "title_ky": "Окуу",
            "title_ru": "Чтение",
            "question_count": 2,
            "time_limit_sec": 300,
        },
    ],
}
# math: 0..3 correct -> 0..60 points (20 each); reading: 0..2 -> 0..40 (20 each)
SCALE = {
    "kind": "estimated",
    "source": "test table",
    "points": [
        {"section_code": "math", "raw": 0, "scaled": 0},
        {"section_code": "math", "raw": 3, "scaled": 60},
        {"section_code": "reading", "raw": 0, "scaled": 0},
        {"section_code": "reading", "raw": 2, "scaled": 40},
    ],
}


async def make_format(client: AsyncClient, staff: Staff, *, with_scale: bool = True) -> dict:
    created = await client.post(f"{ADMIN}/exam-formats", json=FORMAT, headers=staff.admin)
    assert created.status_code == 201, created.text
    fv = created.json()
    published = await client.post(f"{ADMIN}/exam-format-versions/{fv['id']}/publish", headers=staff.admin)
    assert published.status_code == 200, published.text
    if with_scale:
        scale = await client.post(
            f"{ADMIN}/exam-format-versions/{fv['id']}/scales", json=SCALE, headers=staff.admin
        )
        assert scale.status_code == 201, scale.text
        done = await client.post(f"{ADMIN}/score-scales/{scale.json()['id']}/publish", headers=staff.admin)
        assert done.status_code == 200, done.text
    return fv


async def make_mock(
    client: AsyncClient, staff: Staff, code: str = "mock-1", *, with_scale: bool = True
) -> dict:
    """A published mock exam: 3 math + 2 reading questions, the correct option is always B."""
    skill = await make_skill(client, staff)
    fv = await make_format(client, staff, with_scale=with_scale)
    questions = []
    for section, count in (("math", 3), ("reading", 2)):
        for _ in range(count):
            version = await make_question(client, staff, skill["id"])
            questions.append({"question_version_id": version["id"], "section_code": section})
    created = await client.post(
        f"{ADMIN}/tests",
        json={
            "code": code,
            "title_ky": "ЖРТ сыноо",
            "title_ru": "Пробный ОРТ",
            "kind": "mock",
            "language": "ky",
            "format_version_id": fv["id"],
            "questions": questions,
        },
        headers=staff.editor,
    )
    assert created.status_code == 201, created.text
    published = await client.post(f"{ADMIN}/tests/{created.json()['id']}/publish", headers=staff.admin)
    assert published.status_code == 200, published.text
    return {**published.json(), "skill": skill, "format_version": fv, "questions": questions}


async def make_mini(client: AsyncClient, staff: Staff, skill_id: str, count: int = 2) -> dict:
    questions = [
        {"question_version_id": (await make_question(client, staff, skill_id))["id"]} for _ in range(count)
    ]
    created = await client.post(
        f"{ADMIN}/tests",
        json={
            "code": "mini-1",
            "title_ky": "Мини-тест",
            "title_ru": "Мини-тест",
            "kind": "mini",
            "language": "ky",
            "time_limit_sec": 600,
            "questions": questions,
        },
        headers=staff.editor,
    )
    assert created.status_code == 201, created.text
    published = await client.post(f"{ADMIN}/tests/{created.json()['id']}/publish", headers=staff.admin)
    assert published.status_code == 200, published.text
    return {**published.json(), "questions": questions}


async def time_travel(seconds: int) -> None:
    """Moves every running session into the past, as if `seconds` had gone by."""
    async with engine.begin() as conn:
        for table, columns in (
            ("test_sessions", ("started_at", "deadline_at")),
            ("session_sections", ("started_at", "deadline_at", "closed_at")),
        ):
            assignments = ", ".join(f"{c} = {c} - make_interval(secs => :s)" for c in columns)
            await conn.execute(text(f"UPDATE {table} SET {assignments}"), {"s": seconds})


def option_id(question: dict, key: str) -> str:
    return next(o["id"] for o in question["options"] if o["key"] == key)
