from io import BytesIO

from PIL import Image


LANDING_PHOTOS = {
    "/static/img/landing/hero.png": (2153, 730),
    "/static/img/landing/guest.png": (1536, 1024),
    "/static/img/landing/business.png": (1672, 941),
    "/static/img/landing/start.png": (1254, 1254),
    "/static/img/landing/scan.png": (1358, 1159),
    "/static/img/landing/gift.png": (1358, 1159),
}


def test_favicon(client) -> None:
    ico = client.get("/favicon.ico")
    assert ico.status_code == 200
    assert ico.content[:4] == b"\x00\x00\x01\x00"
    png = client.get("/static/favicon.png")
    assert png.status_code == 200
    assert png.content[:8] == b"\x89PNG\r\n\x1a\n"
    banner = client.get("/static/img/kartych-banner.png")
    assert banner.status_code == 200
    assert banner.content[:8] == b"\x89PNG\r\n\x1a\n"
    home = client.get("/")
    assert "/static/favicon.png" in home.text
    assert "/favicon.ico" in home.text
    assert "/static/img/landing/hero.png" in home.text
    assert "Картыч" in home.text


def test_landing_photos(client) -> None:
    for path, size in LANDING_PHOTOS.items():
        response = client.get(path)
        assert response.status_code == 200, path
        assert response.content[:8] == b"\x89PNG\r\n\x1a\n"
        image = Image.open(BytesIO(response.content))
        assert image.size == size, path
        assert image.mode in {"RGB", "RGBA"}


def test_public_site(client) -> None:
    home = client.get("/")
    assert home.status_code == 200
    assert "Картыч" in home.text
    assert "landing/hero.png" in home.text
    assert "landing/guest.png" in home.text
    assert "landing/business.png" in home.text
    assert "landing/start.png" in home.text
    assert "landing/scan.png" in home.text
    assert "landing/gift.png" in home.text
    assert "Для гостя" in home.text
    assert "Для точки" in home.text
    assert "photo-slot" in home.text
    assert 'href="/login"' in home.text
    assert "В кабинет" in home.text
    assert "site-top-go" in home.text
    assert "Кто ты" not in home.text
    login = client.post("/login/demo", data={"role": "client"}, follow_redirects=True)
    assert login.status_code == 200
    home_after = client.get("/")
    assert home_after.status_code == 200
    assert "Для гостя" in home_after.text
    assert "cup-bar" not in home_after.text
    assert "В кабинет" in home_after.text
    assert 'href="/me"' in home_after.text
    shops = client.get("/me/shops")
    assert shops.status_code == 200
    assert "Точка на Ленина" in shops.text
    assert "Профиль" in shops.text
    assert "Рядом" in shops.text
    assert "Карты" in shops.text


def test_max_login_greeting(client, app) -> None:
    page = client.get("/login")
    assert page.status_code == 200
    assert "Войти через MAX" in page.text
    assert 'name="name"' not in page.text
    wait = client.post("/login", data={"role": "client"})
    assert wait.status_code == 200
    assert "Подтверди в MAX" in wait.text
    token = wait.text.split('data-token="', 1)[1].split('"', 1)[0]
    assert f"?start=c_{token}" in wait.text
    hook = client.post(
        "/webhook",
        json={
            "update_type": "bot_started",
            "timestamp": 1,
            "chat_id": 4242,
            "user": {"user_id": 4242, "name": "Камиль", "username": "kamil"},
            "payload": f"c_{token}",
        },
        headers={"X-Max-Bot-Api-Secret": "secret123"},
    )
    assert hook.status_code == 200
    assert client.get(f"/login/status/{token}").json()["status"] == "ok"
    done = client.get(f"/login/complete/{token}", follow_redirects=True)
    assert done.status_code == 200
    assert "Привет, Камиль" in done.text
    assert "через MAX" in done.text
    home = client.get("/")
    assert home.status_code == 200
    assert "Для гостя" in home.text
    assert "Карты любимых мест" in home.text
    cabinet = client.get("/me")
    assert "Привет, Камиль" in cabinet.text
    kwargs = app.state.max_client.send_message.await_args.kwargs
    assert "подтвержд" in kwargs["text"].lower()
    buttons = kwargs["attachments"][0]["payload"]["buttons"]
    assert buttons[0][0]["type"] == "link"
    assert token in buttons[0][0]["url"]


def test_demo_client_cabinet_and_join(client) -> None:
    login = client.post("/login/demo", data={"role": "client"}, follow_redirects=True)
    assert login.status_code == 200
    assert "Привет, Демо-гость" in login.text
    shops = client.get("/me/shops")
    chunk = shops.text.split("Точка на Ленина", 1)[1]
    shop_id = chunk.split("/me/join/", 1)[1].split('"', 1)[0]
    joined = client.post(f"/me/join/{shop_id}", follow_redirects=True)
    assert joined.status_code == 200
    assert "Точка на Ленина" in joined.text


def test_guest_and_business_bars(client) -> None:
    client.post("/login/demo", data={"role": "client"})
    guest = client.get("/me")
    assert 'href="/me/qr"' in guest.text
    assert 'id="scan-btn"' not in guest.text
    assert "cup-bar" in guest.text
    assert "cup-bar-top" in guest.text
    assert "В кабинет" in guest.text
    client.post("/login/demo", data={"role": "business"})
    biz = client.get("/biz")
    assert "Аналитика" in biz.text
    assert "Игры" in biz.text
    assert 'href="/biz/earn"' in biz.text
    assert 'action="/biz/earn"' in client.get("/biz/earn").text
    contests = client.get("/biz/contests")
    assert "По покупкам" in contests.text
    assert "Бонусы" in contests.text
    assert "Новая ссылка" in contests.text
    assert "/biz/promos/" in contests.text


def test_earn_and_redeem_qr(client) -> None:
    client.post("/login/demo", data={"role": "business"})
    made = client.post(
        "/biz/earn",
        data={"program_id": "", "items": "Кофе", "qty": 1, "amount_rub": 180, "place": "Касса"},
    )
    assert made.status_code == 200
    payload = made.text.split('data-code="', 1)[1].split('"', 1)[0]
    assert payload.startswith("cc1:")

    client.post("/login/demo", data={"role": "client"})
    scanned = client.post("/app/scan", json={"code": payload})
    body = scanned.json()
    assert scanned.status_code == 200
    assert body["ok"] is True
    me = client.get("/me")
    assert "Моя точка" in me.text
