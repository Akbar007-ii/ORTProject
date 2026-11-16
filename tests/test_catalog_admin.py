import uuid

from sqlalchemy import select

from mysite.db.models import AuditLog
from tests.conftest import bearer, register, register_with_role

API = "/api/v1"
ADMIN = f"{API}/admin"

MATH = {"code": "math", "title_ky": "Математика", "title_ru": "Математика", "order": 1}


async def _editor(client) -> dict:
    return bearer(await register_with_role(client, "editor@example.com", "editor"))


async def test_students_and_anonymous_cannot_use_admin(client):
    student = bearer(await register(client))
    for headers, expected in ((student, 403), ({}, 401)):
        assert (await client.post(f"{ADMIN}/subjects", json=MATH, headers=headers)).status_code == expected
        assert (await client.get(f"{ADMIN}/subjects", headers=headers)).status_code == expected
        assert (await client.get(f"{ADMIN}/users", headers=headers)).status_code == expected


async def test_editor_manages_catalog_but_not_users(client):
    editor = await _editor(client)
    assert (await client.post(f"{ADMIN}/subjects", json=MATH, headers=editor)).status_code == 201
    assert (await client.get(f"{ADMIN}/users", headers=editor)).status_code == 403


async def test_subject_code_is_unique(client):
    editor = await _editor(client)
    assert (await client.post(f"{ADMIN}/subjects", json=MATH, headers=editor)).status_code == 201
    duplicate = await client.post(f"{ADMIN}/subjects", json=MATH, headers=editor)
    assert duplicate.status_code == 409
    assert duplicate.json()["error"]["code"] == "code_taken"


async def test_public_tree_shows_only_active_items(client):
    editor = await _editor(client)
    subject = (await client.post(f"{ADMIN}/subjects", json=MATH, headers=editor)).json()
    sid = subject["id"]

    async def topic(title: str, **extra) -> dict:
        payload = {"subject_id": sid, "title_ky": title, "title_ru": title, **extra}
        response = await client.post(f"{ADMIN}/topics", json=payload, headers=editor)
        assert response.status_code == 201, response.text
        return response.json()

    async def skill(topic_id: str, code: str, **extra) -> dict:
        payload = {"topic_id": topic_id, "code": code, "title_ky": code, "title_ru": code, **extra}
        response = await client.post(f"{ADMIN}/skills", json=payload, headers=editor)
        assert response.status_code == 201, response.text
        return response.json()

    algebra = await topic("Алгебра", order=1)
    quadratic = await topic("Квадраттык теңдемелер", parent_id=algebra["id"])
    await skill(quadratic["id"], "math.quadratic.discriminant", order=1)
    await skill(quadratic["id"], "math.quadratic.hidden", active=False)
    hidden_topic = await topic("Жашыруун", order=2, active=False)
    await topic("Жашыруундун баласы", parent_id=hidden_topic["id"])

    # Public endpoints need no login.
    subjects = (await client.get(f"{API}/subjects")).json()
    assert [s["code"] for s in subjects] == ["math"]

    tree = (await client.get(f"{API}/subjects/{sid}/topics")).json()
    assert [t["title_ky"] for t in tree] == ["Алгебра"]
    child = tree[0]["children"][0]
    assert child["title_ky"] == "Квадраттык теңдемелер"
    assert [s["code"] for s in child["skills"]] == ["math.quadratic.discriminant"]

    # Editors see everything, including inactive items.
    admin_tree = (await client.get(f"{ADMIN}/subjects/{sid}/topics", headers=editor)).json()
    assert [t["title_ky"] for t in admin_tree] == ["Алгебра", "Жашыруун"]
    assert len(admin_tree[0]["children"][0]["skills"]) == 2

    # Switching the subject off hides it from students entirely.
    off = await client.patch(f"{ADMIN}/subjects/{sid}", json={"active": False}, headers=editor)
    assert off.status_code == 200
    assert (await client.get(f"{API}/subjects")).json() == []
    assert (await client.get(f"{API}/subjects/{sid}/topics")).status_code == 404


async def test_topic_parent_rules(client):
    editor = await _editor(client)
    math = (await client.post(f"{ADMIN}/subjects", json=MATH, headers=editor)).json()
    other = (await client.post(f"{ADMIN}/subjects", json={**MATH, "code": "kyrgyz"}, headers=editor)).json()

    async def topic(subject_id: str, **extra):
        payload = {"subject_id": subject_id, "title_ky": "T", "title_ru": "T", **extra}
        return await client.post(f"{ADMIN}/topics", json=payload, headers=editor)

    parent = (await topic(math["id"])).json()
    child = (await topic(math["id"], parent_id=parent["id"])).json()

    # Parent from another subject or a missing one.
    assert (await topic(other["id"], parent_id=parent["id"])).status_code == 422
    assert (await topic(math["id"], parent_id=str(uuid.uuid4()))).status_code == 422

    # Cycles: a topic cannot become a child of itself or of its own descendant.
    for new_parent in (parent["id"], child["id"]):
        response = await client.patch(
            f"{ADMIN}/topics/{parent['id']}", json={"parent_id": new_parent}, headers=editor
        )
        assert response.status_code == 422
        assert response.json()["error"]["code"] == "topic_cycle"

    # Moving the child to the top level with an explicit null works.
    moved = await client.patch(f"{ADMIN}/topics/{child['id']}", json={"parent_id": None}, headers=editor)
    assert moved.status_code == 200
    assert moved.json()["parent_id"] is None


async def test_unknown_ids_return_404(client):
    editor = await _editor(client)
    missing = uuid.uuid4()
    assert (await client.get(f"{API}/subjects/{missing}/topics")).status_code == 404
    patch = await client.patch(f"{ADMIN}/topics/{missing}", json={"order": 1}, headers=editor)
    assert patch.status_code == 404
    assert (await client.get(f"{API}/subjects/not-a-uuid/topics")).status_code == 400


async def test_admin_assigns_roles_with_audit(client, db):
    admin = bearer(await register_with_role(client, "admin@example.com", "admin"))
    student = await register(client, "student@example.com")
    student_id = student["user"]["id"]

    response = await client.put(
        f"{ADMIN}/users/{student_id}/roles", json={"roles": ["student", "editor"]}, headers=admin
    )
    assert response.status_code == 200
    assert response.json()["roles"] == ["editor", "student"]

    # The new role works right away with the token the user already has.
    assert (await client.post(f"{ADMIN}/subjects", json=MATH, headers=bearer(student))).status_code == 201

    log = await db.scalar(select(AuditLog).where(AuditLog.action == "user.roles.set"))
    assert log.object_id == student_id
    assert log.meta == {"before": ["student"], "after": ["editor", "student"]}

    listing = (await client.get(f"{ADMIN}/users", headers=admin)).json()
    assert listing["total"] == 2
    assert "password_hash" not in str(listing)


async def test_role_assignment_guards(client):
    admin_auth = await register_with_role(client, "admin@example.com", "admin")
    admin = bearer(admin_auth)
    editor_auth = await register_with_role(client, "editor@example.com", "editor")

    # A non-admin cannot grant roles, not even to themselves.
    self_promote = await client.put(
        f"{ADMIN}/users/{editor_auth['user']['id']}/roles",
        json={"roles": ["admin"]},
        headers=bearer(editor_auth),
    )
    assert self_promote.status_code == 403

    unknown_role = await client.put(
        f"{ADMIN}/users/{editor_auth['user']['id']}/roles", json={"roles": ["superuser"]}, headers=admin
    )
    assert unknown_role.status_code == 400

    own = await client.put(
        f"{ADMIN}/users/{admin_auth['user']['id']}/roles", json={"roles": ["student"]}, headers=admin
    )
    assert own.status_code == 422
    assert own.json()["error"]["code"] == "cannot_remove_own_admin"

    missing = await client.put(
        f"{ADMIN}/users/{uuid.uuid4()}/roles", json={"roles": ["student"]}, headers=admin
    )
    assert missing.status_code == 404


async def test_health(client):
    assert (await client.get("/health")).json() == {"status": "ok"}
    assert (await client.get("/ready")).json() == {"status": "ready"}
