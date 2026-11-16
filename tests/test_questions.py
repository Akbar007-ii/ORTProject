from tests.conftest import bearer, register
from tests.factories import (
    ADMIN,
    make_question,
    make_skill,
    make_staff,
    publish_question,
    question_payload,
    transition,
)

Q = f"{ADMIN}/questions"


async def _draft(client, staff, skill, **overrides) -> dict:
    response = await client.post(Q, json=question_payload(skill["id"], **overrides), headers=staff.editor)
    assert response.status_code == 201, response.text
    return response.json()


async def test_full_review_workflow(client):
    staff = await make_staff(client)
    skill = await make_skill(client, staff)
    draft = await _draft(client, staff, skill)
    assert draft["status"] == "draft"
    assert draft["version"] == 1
    assert draft["code"].startswith("Q-")

    published = await publish_question(client, staff, draft["id"])
    assert published["status"] == "published"
    assert published["published_at"] is not None

    reviews = (await client.get(f"{Q}/versions/{draft['id']}/reviews", headers=staff.expert)).json()
    assert [(r["from_status"], r["to_status"]) for r in reviews] == [
        ("draft", "editorial_review"),
        ("editorial_review", "expert_review"),
        ("expert_review", "approved"),
        ("approved", "published"),
    ]


async def test_question_bank_is_closed_to_students(client):
    staff = await make_staff(client)
    skill = await make_skill(client, staff)
    draft = await _draft(client, staff, skill)
    student = bearer(await register(client))

    assert (await client.get(Q, headers=student)).status_code == 403
    assert (await client.get(f"{Q}/versions/{draft['id']}", headers=student)).status_code == 403
    assert (await client.post(Q, json=question_payload(skill["id"]), headers=student)).status_code == 403
    assert (await client.get(Q)).status_code == 401
    # Experts review but do not write questions.
    assert (await client.post(Q, json=question_payload(skill["id"]), headers=staff.expert)).status_code == 403


async def test_incomplete_question_cannot_enter_review(client):
    staff = await make_staff(client)
    skill = await make_skill(client, staff)
    payload = question_payload(skill["id"])

    cases = {
        "3 options": {"options": payload["options"][:3]},
        "two correct": {"options": [{**o, "is_correct": o["key"] in "AB"} for o in payload["options"]]},
        "no correct": {"options": [{**o, "is_correct": False} for o in payload["options"]]},
        "no solution": {"solution": None},
        "no license": {"license": None},
    }
    for name, overrides in cases.items():
        draft = await _draft(client, staff, skill, **overrides)  # saving a draft is allowed
        response = await transition(client, staff.editor, draft["id"], "editorial_review")
        assert response.status_code == 422, name
        assert response.json()["error"]["code"] == "question_incomplete", name
        assert response.json()["error"]["details"], name


async def test_duplicate_option_keys_are_rejected(client):
    staff = await make_staff(client)
    skill = await make_skill(client, staff)
    options = [{"key": "A", "text": "x", "is_correct": True}, {"key": "A", "text": "y"}]
    response = await client.post(Q, json=question_payload(skill["id"], options=options), headers=staff.editor)
    assert response.status_code == 400


async def test_roles_are_enforced_on_each_step(client):
    staff = await make_staff(client)
    skill = await make_skill(client, staff)
    draft = await _draft(client, staff, skill)
    vid = draft["id"]

    # Skipping steps is not a valid transition for anyone.
    assert (await transition(client, staff.admin, vid, "published")).status_code == 409
    assert (await transition(client, staff.expert, vid, "editorial_review")).status_code == 403

    assert (await transition(client, staff.editor, vid, "editorial_review")).status_code == 200
    assert (await transition(client, staff.editor, vid, "expert_review")).status_code == 200
    assert (
        await transition(client, staff.editor, vid, "approved")
    ).status_code == 403  # editor is not an expert
    assert (await transition(client, staff.expert, vid, "approved")).status_code == 200
    assert (
        await transition(client, staff.expert, vid, "published")
    ).status_code == 403  # only admin publishes
    assert (await transition(client, staff.editor, vid, "published")).status_code == 403
    assert (await transition(client, staff.admin, vid, "published")).status_code == 200
    assert (await transition(client, staff.editor, vid, "retired")).status_code == 403
    assert (await transition(client, staff.admin, vid, "retired")).status_code == 200
    assert (await transition(client, staff.admin, vid, "published")).status_code == 409  # retired is final


async def test_author_cannot_approve_own_question(client):
    staff = await make_staff(client)
    skill = await make_skill(client, staff)
    # The admin writes the question, so the admin may not be the one who approves it.
    created = await client.post(Q, json=question_payload(skill["id"]), headers=staff.admin)
    vid = created.json()["id"]
    await transition(client, staff.admin, vid, "editorial_review")
    await transition(client, staff.admin, vid, "expert_review")

    own = await transition(client, staff.admin, vid, "approved")
    assert own.status_code == 422
    assert own.json()["error"]["code"] == "self_review_forbidden"
    assert (await transition(client, staff.expert, vid, "approved")).status_code == 200


async def test_reject_requires_comment_and_reopens_editing(client):
    staff = await make_staff(client)
    skill = await make_skill(client, staff)
    draft = await _draft(client, staff, skill)
    vid = draft["id"]
    await transition(client, staff.editor, vid, "editorial_review")

    # In review the question is frozen.
    frozen = await client.patch(f"{Q}/versions/{vid}", json={"stem": "changed"}, headers=staff.editor)
    assert frozen.status_code == 409

    await transition(client, staff.editor, vid, "expert_review")
    no_comment = await transition(client, staff.expert, vid, "draft")
    assert no_comment.status_code == 422
    assert no_comment.json()["error"]["code"] == "comment_required"
    rejected = await transition(client, staff.expert, vid, "draft", "В варианте C опечатка")
    assert rejected.status_code == 200

    # Back in draft: editable again, including the options.
    new_options = [{"key": k, "text": f"Жаңы {k}", "is_correct": k == "D"} for k in "ABCD"]
    edited = await client.patch(
        f"{Q}/versions/{vid}", json={"stem": "Оңдолгон суроо", "options": new_options}, headers=staff.editor
    )
    assert edited.status_code == 200, edited.text
    body = edited.json()
    assert body["stem"] == "Оңдолгон суроо"
    assert [o["text"] for o in body["options"]] == ["Жаңы A", "Жаңы B", "Жаңы C", "Жаңы D"]
    assert [o["key"] for o in body["options"] if o["is_correct"]] == ["D"]


async def test_published_question_is_immutable_and_fixed_by_new_version(client):
    staff = await make_staff(client)
    skill = await make_skill(client, staff)
    v1 = await make_question(client, staff, skill["id"])

    assert (
        await client.patch(f"{Q}/versions/{v1['id']}", json={"stem": "x"}, headers=staff.admin)
    ).status_code == 409

    created = await client.post(f"{Q}/versions/{v1['id']}/new-version", headers=staff.editor)
    assert created.status_code == 201
    v2 = created.json()
    assert (v2["version"], v2["status"], v2["group_id"], v2["code"]) == (
        2,
        "draft",
        v1["group_id"],
        v1["code"],
    )
    assert v2["stem"] == v1["stem"]

    # Only one unfinished version at a time.
    assert (
        await client.post(f"{Q}/versions/{v1['id']}/new-version", headers=staff.editor)
    ).status_code == 409

    await client.patch(f"{Q}/versions/{v2['id']}", json={"stem": "Туураланган"}, headers=staff.editor)
    await publish_question(client, staff, v2["id"])

    # Publishing v2 retires v1, and v1's text is untouched.
    old = (await client.get(f"{Q}/versions/{v1['id']}", headers=staff.editor)).json()
    assert old["status"] == "retired"
    assert old["stem"] == v1["stem"]
    published = (await client.get(Q, params={"status": "published"}, headers=staff.editor)).json()
    assert [(i["version"], i["stem"]) for i in published["items"]] == [(2, "Туураланган")]


async def test_second_language_joins_the_same_group(client):
    staff = await make_staff(client)
    skill = await make_skill(client, staff)
    ky = await _draft(client, staff, skill)

    payload = question_payload(skill["id"], language="ru", stem="Решите уравнение")
    del payload["skill_id"]
    ru = await client.post(Q, json={**payload, "group_id": ky["group_id"]}, headers=staff.editor)
    assert ru.status_code == 201, ru.text
    assert (ru.json()["code"], ru.json()["language"], ru.json()["version"]) == (ky["code"], "ru", 1)

    again = await client.post(Q, json={**payload, "group_id": ky["group_id"]}, headers=staff.editor)
    assert again.status_code == 409

    both = await client.post(
        Q, json={**payload, "skill_id": skill["id"], "group_id": ky["group_id"]}, headers=staff.editor
    )
    assert both.status_code == 400

    ru_only = (await client.get(Q, params={"language": "ru"}, headers=staff.editor)).json()
    assert ru_only["total"] == 1
