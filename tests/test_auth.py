from datetime import UTC, datetime, timedelta

from sqlalchemy import select, update

from mysite.db.models import AuthSession, User, UserConsent
from tests.conftest import PASSWORD, bearer, register, register_payload

API = "/api/v1"


async def test_register_creates_student_with_consents(client, db):
    auth = await register(client, "  Aibek@Example.com ")

    assert auth["user"]["email"] == "aibek@example.com"  # normalized
    assert auth["user"]["roles"] == ["student"]
    assert auth["user"]["locale"] == "ky"
    assert "password" not in str(auth)

    user = await db.scalar(select(User).where(User.email == "aibek@example.com"))
    assert user.password_hash.startswith("$argon2id$")
    consents = (await db.scalars(select(UserConsent).where(UserConsent.user_id == user.id))).all()
    assert {(c.type, c.document_version) for c in consents} == {("terms", "2026-11"), ("privacy", "2026-11")}


async def test_register_duplicate_email_conflicts(client):
    await register(client)
    response = await client.post(f"{API}/auth/register", json=register_payload("AIBEK@example.com"))
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "email_taken"


async def test_register_requires_consent_and_strong_password(client):
    no_consent = await client.post(f"{API}/auth/register", json=register_payload(accept_terms=False))
    assert no_consent.status_code == 400

    weak = await client.post(f"{API}/auth/register", json=register_payload(password="short"))
    assert weak.status_code == 400
    body = weak.json()["error"]
    assert body["code"] == "validation_error"
    assert body["details"][0]["field"] == "password"
    assert "short" not in weak.text  # submitted values are never echoed back
    assert body["request_id"] == weak.headers["x-request-id"]


async def test_register_cannot_set_roles(client):
    response = await client.post(f"{API}/auth/register", json=register_payload(roles=["admin"]))
    assert response.status_code == 400


async def test_login_and_me(client):
    await register(client)
    response = await client.post(
        f"{API}/auth/login", json={"email": "aibek@example.com", "password": PASSWORD}
    )
    assert response.status_code == 200
    auth = response.json()

    me = await client.get(f"{API}/me", headers=bearer(auth))
    assert me.status_code == 200
    assert me.json()["email"] == "aibek@example.com"


async def test_login_failure_does_not_reveal_whether_email_exists(client):
    await register(client)
    wrong = await client.post(
        f"{API}/auth/login", json={"email": "aibek@example.com", "password": "wrong-password"}
    )
    unknown = await client.post(
        f"{API}/auth/login", json={"email": "nobody@example.com", "password": "wrong-password"}
    )

    assert wrong.status_code == unknown.status_code == 401
    assert wrong.json()["error"]["code"] == unknown.json()["error"]["code"] == "invalid_credentials"
    assert wrong.json()["error"]["message"] == unknown.json()["error"]["message"]


async def test_login_brute_force_is_rate_limited(client):
    await register(client)
    creds = {"email": "aibek@example.com", "password": "wrong-password"}
    for _ in range(5):
        assert (await client.post(f"{API}/auth/login", json=creds)).status_code == 401

    blocked = await client.post(f"{API}/auth/login", json=creds)
    assert blocked.status_code == 429
    assert int(blocked.headers["retry-after"]) > 0

    # Even the right password is refused while the limit is active.
    right = await client.post(f"{API}/auth/login", json={**creds, "password": PASSWORD})
    assert right.status_code == 429


async def test_successful_login_resets_attempt_counter(client):
    await register(client)
    good = {"email": "aibek@example.com", "password": PASSWORD}
    bad = {**good, "password": "wrong-password"}
    for _ in range(3):
        for _ in range(4):
            assert (await client.post(f"{API}/auth/login", json=bad)).status_code == 401
        assert (await client.post(f"{API}/auth/login", json=good)).status_code == 200


async def test_me_requires_valid_token(client):
    assert (await client.get(f"{API}/me")).status_code == 401
    bad = await client.get(f"{API}/me", headers={"Authorization": "Bearer not-a-jwt"})
    assert bad.status_code == 401
    assert bad.json()["error"]["code"] == "invalid_token"


async def test_refresh_issues_working_access_token(client):
    auth = await register(client)
    response = await client.post(
        f"{API}/auth/refresh", json={"refresh_token": auth["tokens"]["refresh_token"]}
    )
    assert response.status_code == 200
    new = {"tokens": response.json()}
    assert (await client.get(f"{API}/me", headers=bearer(new))).status_code == 200

    bad = await client.post(f"{API}/auth/refresh", json={"refresh_token": "unknown"})
    assert bad.status_code == 401


async def test_expired_session_cannot_refresh_or_access(client, db):
    auth = await register(client)
    await db.execute(update(AuthSession).values(expires_at=datetime.now(UTC) - timedelta(seconds=1)))
    await db.commit()

    refresh = await client.post(
        f"{API}/auth/refresh", json={"refresh_token": auth["tokens"]["refresh_token"]}
    )
    assert refresh.status_code == 401
    assert (await client.get(f"{API}/me", headers=bearer(auth))).status_code == 401


async def test_logout_revokes_session_immediately(client):
    auth = await register(client)
    refresh_token = auth["tokens"]["refresh_token"]

    assert (await client.post(f"{API}/auth/logout", json={"refresh_token": refresh_token})).status_code == 204
    # The still-unexpired access token stops working, and so does the refresh token.
    assert (await client.get(f"{API}/me", headers=bearer(auth))).status_code == 401
    assert (
        await client.post(f"{API}/auth/refresh", json={"refresh_token": refresh_token})
    ).status_code == 401
    # Logout is idempotent.
    assert (await client.post(f"{API}/auth/logout", json={"refresh_token": refresh_token})).status_code == 204


async def test_logout_all_ends_every_device(client):
    phone = await register(client)
    laptop = (
        await client.post(f"{API}/auth/login", json={"email": "aibek@example.com", "password": PASSWORD})
    ).json()

    assert (await client.post(f"{API}/auth/logout-all", headers=bearer(phone))).status_code == 204
    assert (await client.get(f"{API}/me", headers=bearer(phone))).status_code == 401
    assert (await client.get(f"{API}/me", headers=bearer(laptop))).status_code == 401


async def test_blocked_user_loses_access(client, db):
    auth = await register(client)
    await db.execute(update(User).values(status="blocked"))
    await db.commit()

    assert (await client.get(f"{API}/me", headers=bearer(auth))).status_code == 401
    login = await client.post(f"{API}/auth/login", json={"email": "aibek@example.com", "password": PASSWORD})
    assert login.status_code == 401
    refresh = await client.post(
        f"{API}/auth/refresh", json={"refresh_token": auth["tokens"]["refresh_token"]}
    )
    assert refresh.status_code == 401


async def test_password_reset_flow(client, reset_tokens):
    old = await register(client)
    new_password = "brand-new-password-42"

    # Unknown and known emails get the same answer.
    unknown = await client.post(f"{API}/auth/password-reset/request", json={"email": "nobody@example.com"})
    known = await client.post(f"{API}/auth/password-reset/request", json={"email": "aibek@example.com"})
    assert unknown.status_code == known.status_code == 202
    assert unknown.json() == known.json()
    assert list(reset_tokens) == ["aibek@example.com"]
    token = reset_tokens["aibek@example.com"]

    confirm = await client.post(
        f"{API}/auth/password-reset/confirm", json={"token": token, "new_password": new_password}
    )
    assert confirm.status_code == 204

    # Old sessions are gone, old password no longer works, new one does.
    assert (await client.get(f"{API}/me", headers=bearer(old))).status_code == 401
    creds = {"email": "aibek@example.com", "password": PASSWORD}
    assert (await client.post(f"{API}/auth/login", json=creds)).status_code == 401
    assert (
        await client.post(f"{API}/auth/login", json={**creds, "password": new_password})
    ).status_code == 200

    # The token is single-use.
    again = await client.post(
        f"{API}/auth/password-reset/confirm", json={"token": token, "new_password": "another-password-1"}
    )
    assert again.status_code == 422
    assert again.json()["error"]["code"] == "invalid_reset_token"


async def test_expired_reset_token_is_rejected(client, db, reset_tokens):
    from mysite.db.models import PasswordResetToken

    await register(client)
    await client.post(f"{API}/auth/password-reset/request", json={"email": "aibek@example.com"})
    await db.execute(update(PasswordResetToken).values(expires_at=datetime.now(UTC) - timedelta(seconds=1)))
    await db.commit()

    response = await client.post(
        f"{API}/auth/password-reset/confirm",
        json={"token": reset_tokens["aibek@example.com"], "new_password": "brand-new-password-42"},
    )
    assert response.status_code == 422
    login = await client.post(f"{API}/auth/login", json={"email": "aibek@example.com", "password": PASSWORD})
    assert login.status_code == 200  # password unchanged


async def test_update_me(client):
    auth = await register(client)
    response = await client.patch(
        f"{API}/me",
        headers=bearer(auth),
        json={"display_name": "Айбек", "locale": "ru", "timezone": "Asia/Almaty", "grade_status": "grade_11"},
    )
    assert response.status_code == 200
    body = response.json()
    assert (body["display_name"], body["locale"], body["timezone"], body["grade_status"]) == (
        "Айбек",
        "ru",
        "Asia/Almaty",
        "grade_11",
    )

    bad_tz = await client.patch(f"{API}/me", headers=bearer(auth), json={"timezone": "Mars/Olympus"})
    assert bad_tz.status_code == 400

    # Privilege escalation through the profile is impossible: unknown fields are rejected.
    escalate = await client.patch(f"{API}/me", headers=bearer(auth), json={"roles": ["admin"]})
    assert escalate.status_code == 400
    assert (await client.get(f"{API}/me", headers=bearer(auth))).json()["roles"] == ["student"]
