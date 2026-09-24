from tests.test_session import signed_launch

from app.web.loyalty import guest_payload, promo_start_payload, staff_start_payload


def test_guest_qr_page(client) -> None:
    client.post("/login/demo", data={"role": "client"})
    page = client.get("/me/qr")
    assert page.status_code == 200
    assert "cupuser:-11:" in page.text
    assert "Мой QR" in page.text


def test_staff_invite_scan_and_charge(client, app) -> None:
    client.post("/login/demo", data={"role": "business"})
    settings = client.get("/settings")
    assert "Кассиры" in settings.text
    invited = client.post(
        "/biz/staff/invite",
        data={"can_stats": "on", "can_earn": "on", "can_scan": "on"},
        follow_redirects=False,
    )
    assert invited.status_code == 303
    page = client.get("/settings")
    token = page.text.split("/join/", 1)[1].split("<", 1)[0].split('"', 1)[0].strip()
    assert len(token) == 16
    assert f"?start=s_{token}" in page.text

    client.post("/login/demo", data={"role": "client"})
    joined = client.get(f"/join/{token}", follow_redirects=True)
    assert joined.status_code == 200
    assert "Сканер" in joined.text or "кассир" in joined.text.lower()
    earn = client.get("/biz/earn")
    assert earn.status_code == 200
    assert "Создать QR" in earn.text
    denied = client.post(
        "/biz/promo",
        data={"title": "Секрет", "kind": "visits", "goal": 3},
        follow_redirects=False,
    )
    assert denied.status_code == 303
    assert denied.headers["location"] == "/biz"

    guest = signed_launch(app.state.settings.max_bot_token, user='{"id": 99, "first_name": "Гость"}')
    auth = client.post("/app/auth", json={"init_data": guest})
    assert auth.status_code == 200
    qr = client.get("/me/qr")
    assert qr.status_code == 200
    payload = guest_payload("secret123", 99)
    assert payload in qr.text

    client.post("/login/demo", data={"role": "client"})
    scanned = client.post("/app/scan", json={"code": payload})
    body = scanned.json()
    assert scanned.status_code == 200
    assert body["ok"] is True
    assert body["next"] == "/biz/charge/99"
    form = client.get("/biz/charge/99")
    assert form.status_code == 200
    assert "Гость" in form.text
    charged = client.post(
        "/biz/charge/99",
        data={"program_id": "", "items": "Кофе", "qty": 1, "amount_rub": 180, "place": "Касса"},
        follow_redirects=True,
    )
    assert charged.status_code == 200
    assert "записана" in charged.text.lower() or "Моя точка" in charged.text

    client.post("/app/auth", json={"init_data": guest})
    cards = client.get("/me")
    assert "Моя точка" in cards.text


def test_staff_max_start_invite(client, app) -> None:
    client.post("/login/demo", data={"role": "business"})
    client.post(
        "/biz/staff/invite",
        data={"can_stats": "on", "can_earn": "on", "can_scan": "on", "can_edit": "on"},
    )
    page = client.get("/settings")
    token = page.text.split("/join/", 1)[1].split("<", 1)[0].split('"', 1)[0].strip()
    hook = client.post(
        "/webhook",
        json={
            "update_type": "bot_started",
            "timestamp": 1,
            "chat_id": 77,
            "user": {"user_id": 77, "name": "Кассир", "username": "kassir"},
            "payload": staff_start_payload(token),
        },
        headers={"X-Max-Bot-Api-Secret": "secret123"},
    )
    assert hook.status_code == 200
    kwargs = app.state.max_client.send_message.await_args.kwargs
    assert "кассир" in kwargs["text"].lower()
    launch = signed_launch(
        app.state.settings.max_bot_token,
        user='{"id": 77, "first_name": "Кассир", "username": "kassir"}',
    )
    client.post("/app/auth", json={"init_data": launch})
    scan = client.get("/biz/scan")
    assert scan.status_code == 200
    promos = client.get("/biz/promos")
    assert promos.status_code == 200


def test_promo_link_join_and_qr(client, app) -> None:
    client.post("/login/demo", data={"role": "business"})
    page = client.get("/biz/promos")
    program_id = page.text.split("/biz/promos/", 1)[1].split("/link", 1)[0]
    first = client.post(f"/biz/promos/{program_id}/link", follow_redirects=True)
    assert first.status_code == 200
    token = first.text.split("Гостям: ", 1)[1].split("/promo/", 1)[1].split("<", 1)[0].strip()
    assert len(token) == 16
    second = client.post(f"/biz/promos/{program_id}/link", follow_redirects=True)
    found = [
        chunk.split("/promo/", 1)[1].split("<", 1)[0].strip()
        for chunk in second.text.split("Гостям: ")[1:]
    ]
    assert token in found
    other = next(item for item in found if item != token)
    own = client.get(f"/promo/{token}")
    assert own.status_code == 200
    assert "Это твоя акция" in own.text

    client.get("/logout")
    public = client.get(f"/promo/{token}")
    assert public.status_code == 200
    assert "Участвовать" in public.text
    assert f"/login?promo={token}" in public.text

    client.post("/login/demo", data={"role": "client"})
    joined = client.post(f"/promo/{token}/join", follow_redirects=True)
    assert joined.status_code == 200
    assert "cupuser:-11:" in joined.text
    assert "Моя точка" in client.get("/me").text

    hook = client.post(
        "/webhook",
        json={
            "update_type": "bot_started",
            "timestamp": 2,
            "chat_id": 88,
            "user": {"user_id": 88, "name": "Лена", "username": "lena"},
            "payload": promo_start_payload(other),
        },
        headers={"X-Max-Bot-Api-Secret": "secret123"},
    )
    assert hook.status_code == 200
    text = app.state.max_client.send_message.await_args.kwargs["text"].lower()
    assert "акции" in text or "участв" in text or "точка" in text

