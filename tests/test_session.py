import hashlib
import hmac
import json
import time
from unittest.mock import AsyncMock
from urllib.parse import quote

from fastapi.testclient import TestClient

from app.main import create_app
from app.web.auth import TTL, verify_init_data


def signed_launch(token, **overrides):
    fields = {
        "auth_date": str(int(time.time())),
        "query_id": "new-launch",
        "user": json.dumps({"id": 12345, "first_name": "Анна"}, ensure_ascii=False),
    }
    fields.update(overrides)
    raw = "\n".join(f"{key}={value}" for key, value in sorted(fields.items()))
    key = hmac.new(b"WebAppData", token.encode(), hashlib.sha256).digest()
    signature = hmac.new(key, raw.encode(), hashlib.sha256).hexdigest()
    return "&".join(f"{key}={quote(value)}" for key, value in fields.items()) + "&hash=" + signature


def test_signed_login_restores_after_cookie_loss_and_remembers_role(client, app):
    launch = signed_launch(app.state.settings.max_bot_token)
    response = client.post("/app/auth", json={"init_data": launch})
    assert response.status_code == 200
    assert f"Max-Age={TTL}" in response.headers["set-cookie"]
    assert client.get("/app/session").json()["role"] == "client"
    home = client.get("/", follow_redirects=False)
    assert home.status_code == 200
    assert "Для гостя" in home.text
    launcher = client.get("/app", follow_redirects=False)
    assert launcher.status_code == 200
    qr = client.get("/app?start=qr", follow_redirects=False)
    assert qr.status_code == 303
    assert qr.headers["location"] == "/me/qr"
    startapp = client.get("/app?startapp=qr", follow_redirects=False)
    assert startapp.status_code == 303
    assert startapp.headers["location"] == "/me/qr"
    mine = client.get("/qr", follow_redirects=False)
    assert mine.status_code == 303
    assert mine.headers["location"] == "/me/qr"
    cabinet = client.get("/app?start=cabinet", follow_redirects=False)
    assert cabinet.status_code == 303
    assert cabinet.headers["location"] == "/me"
    from_root = client.get("/?WebAppStartParam=cabinet", follow_redirects=False)
    assert from_root.status_code == 303
    assert from_root.headers["location"] == "/me"
    login = client.get("/login", follow_redirects=False)
    assert login.headers["location"] == "/me"
    client.cookies.clear()
    assert client.get("/app/session").status_code == 401
    restored = client.post("/app/auth", json={"init_data": launch})
    assert restored.json()["role"] == "client"
    assert restored.json()["next"] == "/me"
    assert client.get("/me").status_code == 200


def test_secure_cookie_supports_embedded_max_and_logout(settings):
    with TestClient(create_app(settings), base_url="https://example.test") as client:
        response = client.post(
            "/app/auth", json={"init_data": signed_launch(settings.max_bot_token)}
        )
        cookie = response.headers["set-cookie"]
        for flag in ["Secure", "HttpOnly", "SameSite=none", "Partitioned"]:
            assert flag in cookie
        assert client.get("/app/session").status_code == 200
        assert client.get("/app/session").headers["cache-control"] == "no-store"
        response = client.get("/logout", follow_redirects=False)
        assert any(
            "Partitioned" in h and "Max-Age=0" in h for h in response.headers.get_list("set-cookie")
        )
        assert client.get("/app/session").status_code == 401


def test_web_login_cookie_is_first_party(settings):
    app = create_app(settings)
    with TestClient(app, base_url="https://example.test") as client:
        app.state.max_client.send_message = AsyncMock(return_value={"ok": True})
        wait = client.post("/login", data={"role": "client"})
        token = wait.text.split('data-token="', 1)[1].split('"', 1)[0]
        client.post(
            "/webhook",
            json={
                "update_type": "bot_started",
                "timestamp": 1,
                "chat_id": 77,
                "user": {"user_id": 77, "name": "Гость"},
                "payload": f"c_{token}",
            },
            headers={"X-Max-Bot-Api-Secret": settings.webhook_secret},
        )
        done = client.get(f"/login/complete/{token}", follow_redirects=False)
        cookie = done.headers["set-cookie"]
        assert "Secure" in cookie
        assert "HttpOnly" in cookie
        assert "SameSite=lax" in cookie
        assert "Partitioned" not in cookie
        assert done.headers["location"] == "/me"


def test_cross_site_write_rejected(client):
    client.post("/login/demo", data={"role": "client"})
    assert (
        client.post(
            "/settings", data={"role": "business"}, headers={"Origin": "https://untrusted.example"}
        ).status_code
        == 403
    )
    assert client.get("/app/session").json()["role"] == "client"
    assert (
        client.post(
            "/settings", data={"role": "business"}, headers={"Origin": "http://testserver"}
        ).status_code
        == 200
    )


def test_invalid_launch_does_not_create_session(client, app):
    token = app.state.settings.max_bot_token
    for raw in [
        signed_launch(token, auth_date="bad"),
        signed_launch(token, auth_date="0"),
        signed_launch(token, user='{"id":"bad"}'),
        signed_launch(token, auth_date=str(int(time.time()) - 90000)),
        signed_launch(token) + "&user=%7B%7D",
    ]:
        assert verify_init_data(raw, token) is None
        assert client.post("/app/auth", json={"init_data": raw}).status_code == 401
    assert client.get("/app/session").status_code == 401


def test_signed_qr_payload_returns_guest_qr_next(client, app):
    launch = signed_launch(app.state.settings.max_bot_token, start_param="qr")
    response = client.post("/app/auth", json={"init_data": launch})
    assert response.status_code == 200
    assert response.json()["next"] == "/me/qr"


def test_signed_cabinet_payload_returns_guest_home(client, app):
    launch = signed_launch(app.state.settings.max_bot_token, start_param="cabinet")
    response = client.post("/app/auth", json={"init_data": launch})
    assert response.status_code == 200
    assert response.json()["next"] == "/me"


def test_miniapp_startapp_completes_website_login(client, app):
    wait = client.post("/login", data={"role": "client"})
    token = wait.text.split('data-token="', 1)[1].split('"', 1)[0]
    launch = signed_launch(
        app.state.settings.max_bot_token,
        start_param=f"c_{token}",
        user='{"id": 5151, "first_name": "Олег"}',
    )
    response = client.post("/app/auth", json={"init_data": launch})
    assert response.status_code == 200
    assert client.get(f"/login/status/{token}").json()["status"] == "ok"
    client.cookies.clear()
    done = client.get(f"/login/complete/{token}", follow_redirects=True)
    assert "Привет, Олег" in done.text


def test_signed_admin_payload_opens_review_queue(client, app):
    launch = signed_launch(
        app.state.settings.max_bot_token,
        start_param="admin",
        user='{"id": 99001, "first_name": "Админ"}',
    )
    response = client.post("/app/auth", json={"init_data": launch})
    assert response.status_code == 200
    assert response.json()["next"] == "/admin"
    opened = client.get("/app?start=admin", follow_redirects=False)
    assert opened.status_code == 303
    assert opened.headers["location"] == "/admin"
    guest = signed_launch(app.state.settings.max_bot_token, start_param="admin")
    denied = client.post("/app/auth", json={"init_data": guest})
    assert denied.json()["next"] == "/me"
