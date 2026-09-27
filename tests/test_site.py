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
    assert "theme-toggle" in home.text
    assert "Как это работает" in home.text
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
    assert "Зарегистрироваться" not in page.text
    assert 'name="name"' not in page.text
    wait = client.post("/login", data={"role": "client"})
    assert wait.status_code == 200
    assert "Подтверди в MAX" in wait.text
    token = wait.text.split('data-token="', 1)[1].split('"', 1)[0]
    assert f"?start=c_{token}" in wait.text
    assert f'data-max-url="https://max.ru/' in wait.text
    assert "Открыть MAX →" not in wait.text
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
    assert 'href="/qr"' in guest.text
    assert 'id="scan-btn"' not in guest.text
    assert "cup-bar" in guest.text
    assert "cup-bar-top" not in guest.text
    assert "Поиск карт" in guest.text or "Найти карту" in guest.text
    assert "В кабинет" in guest.text
    client.post("/login/demo", data={"role": "business"})
    biz = client.get("/biz")
    assert "Аналитика" in biz.text
    assert "Игры" in biz.text
    assert 'href="/biz/promos"' in biz.text
    assert 'href="/qr"' in biz.text
    assert 'href="/biz/scan"' in biz.text
    assert 'id="scan-btn"' in biz.text
    assert "Гостей" in biz.text
    assert "Средний чек" in biz.text
    assert 'action="/biz/earn"' in client.get("/biz/earn").text
    contests = client.get("/biz/contests")
    assert "По покупкам" in contests.text
    assert "Бонусы" in contests.text
    assert "Новая ссылка" in contests.text
    assert "/biz/promos/" in contests.text
    assert "Удалить акцию" in contests.text
    assert "Каталог игр" in contests.text
    assert "game-tile" in contests.text
    assert "Stamp Me" not in contests.text
    staff = client.get("/biz/staff")
    assert staff.status_code == 200
    assert "Пригласить кассира" in staff.text


def test_owner_switches_to_guest_cabinet(client) -> None:
    client.post("/login/demo", data={"role": "business"})
    biz = client.get("/biz")
    assert "Как гость" in biz.text
    guest = client.get("/as/guest", follow_redirects=True)
    assert guest.status_code == 200
    assert "Мои карты" in guest.text
    assert "К точке" in guest.text
    assert 'href="/qr"' in guest.text
    assert "Аналитика" not in guest.text.split("cup-bar", 1)[-1]
    back = client.get("/as/biz", follow_redirects=True)
    assert back.status_code == 200
    assert "Аналитика" in back.text
    assert "Как гость" in back.text


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


def test_business_apply_waits_for_admin(client, app) -> None:
    from tests.test_session import signed_launch

    client.post("/login/demo", data={"role": "client"})
    page = client.get("/biz/apply")
    assert page.status_code == 200
    assert "Открыть точку" in page.text
    sent = client.post(
        "/biz/apply",
        data={
            "name": "Новая кофейня",
            "city": "Казань",
            "address": "Баумана, 1",
            "website": "https://coffee.test",
            "inn": "7707083893",
            "director_name": "Иванов Иван Иванович",
            "latitude": "55.7963",
            "longitude": "49.1088",
        },
        follow_redirects=True,
    )
    assert sent.status_code == 200
    assert "проверке" in sent.text.lower() or "отправили" in sent.text.lower()
    assert "Новая кофейня" not in client.get("/me/shops").text
    denied = client.get("/biz", follow_redirects=False)
    assert denied.status_code == 303
    assert denied.headers["location"] == "/biz/apply"
    notes = [
        call
        for call in app.state.max_client.send_message.await_args_list
        if call.kwargs.get("user_id") == 99001
    ]
    assert notes
    assert "Новая заявка" in notes[0].kwargs["text"]
    assert "/admin" not in notes[0].kwargs["text"]
    button = notes[0].kwargs["attachments"][0]["payload"]["buttons"][0][0]
    assert button["type"] == "open_app"
    assert button["payload"] == "admin"
    assert button["text"] == "Открыть заявку"
    guest = signed_launch(app.state.settings.max_bot_token, user='{"id": 99001, "first_name": "Админ"}')
    assert client.post("/app/auth", json={"init_data": guest}).status_code == 200
    admin = client.get("/admin")
    assert admin.status_code == 200
    assert "Новая кофейня" in admin.text
    shop_id = admin.text.split("/admin/", 1)[1].split("/review", 1)[0]
    approved = client.post(f"/admin/{shop_id}/review", data={"action": "approve"}, follow_redirects=True)
    assert approved.status_code == 200
    client.post("/login/demo", data={"role": "client"})
    cabinet = client.get("/biz")
    assert cabinet.status_code == 200
    assert "Аналитика" in cabinet.text
    assert "Новая кофейня" in client.get("/me/shops").text
    assert "Переключить кабинет" not in client.get("/settings").text


def test_admin_can_search_shop_and_open_access(client, app) -> None:
    from tests.test_session import signed_launch

    client.post("/login/demo", data={"role": "client"})
    client.post(
        "/biz/apply",
        data={
            "name": "Водная кофейня",
            "city": "Казань",
            "address": "Кремлёвская, 2",
            "website": "https://water-coffee.test",
            "inn": "7707083893",
            "director_name": "Петров Пётр Петрович",
            "latitude": "55.7963",
            "longitude": "49.1088",
        },
    )
    guest = signed_launch(app.state.settings.max_bot_token, user='{"id": 99001, "first_name": "Админ"}')
    assert client.post("/app/auth", json={"init_data": guest}).status_code == 200
    settings = client.get("/settings")
    assert "Заявки" in settings.text
    found = client.get("/admin?q=кофейни")
    assert found.status_code == 200
    assert "Водная кофейня" in found.text
    assert "Одобрить" in found.text
    miss = client.get("/admin?q=пекарня")
    assert "Водная кофейня" not in miss.text
    shop_id = found.text.split("/admin/", 1)[1].split("/review", 1)[0]
    opened = client.post(f"/admin/{shop_id}/review", data={"action": "approve"}, follow_redirects=True)
    assert opened.status_code == 200
    again = client.get("/admin?q=кофейня")
    assert "Водная кофейня" in again.text
    assert "подтверждена" in again.text
    assert f"/admin/{shop_id}/review" not in again.text


def test_admin_can_add_another_admin(client, app) -> None:
    from tests.test_session import signed_launch

    first = signed_launch(app.state.settings.max_bot_token, user='{"id": 99001, "first_name": "Админ"}')
    assert client.post("/app/auth", json={"init_data": first}).status_code == 200
    page = client.get("/admin")
    assert page.status_code == 200
    assert "Кто заходит сюда" in page.text
    assert "99001" in page.text
    added = client.post("/admin/admins", data={"max_user_id": 99002}, follow_redirects=True)
    assert added.status_code == 200
    assert "добавлен" in added.text.lower()
    pinned = client.post("/admin/admins/99001/remove", follow_redirects=True)
    assert "настройках сервера" in pinned.text
    second = signed_launch(app.state.settings.max_bot_token, user='{"id": 99002, "first_name": "Второй"}')
    assert client.post("/app/auth", json={"init_data": second}).status_code == 200
    assert client.get("/admin").status_code == 200
    client.post("/app/auth", json={"init_data": first})
    removed = client.post("/admin/admins/99002/remove", follow_redirects=True)
    assert "снят" in removed.text.lower()
    client.post("/app/auth", json={"init_data": second})
    denied = client.get("/admin", follow_redirects=False)
    assert denied.status_code == 200
    assert "не админ" in denied.text.lower() or "нет в списке" in denied.text


def test_first_user_claims_empty_admin_panel() -> None:
    from unittest.mock import AsyncMock

    from fastapi.testclient import TestClient

    from app.config import Settings
    from app.main import create_app
    from tests.test_session import signed_launch

    settings = Settings(
        app_env="test",
        max_bot_token="test-token",
        max_bot_username="cupcard_bot",
        webhook_secret="secret123",
        public_base_url="https://example.test",
        subscribe_on_startup=False,
        admin_max_user_ids="",
    )
    app = create_app(settings)
    with TestClient(app) as client:
        app.state.max_client.send_message = AsyncMock(return_value={"ok": True})
        launch = signed_launch(
            settings.max_bot_token, user='{"id": 4242, "first_name": "Хозяин"}'
        )
        assert client.post("/app/auth", json={"init_data": launch}).status_code == 200
        page = client.get("/admin")
        assert page.status_code == 200
        assert "Стать админом" in page.text
        assert "Мои карты" not in page.text
        taken = client.post("/admin/claim", follow_redirects=True)
        assert taken.status_code == 200
        assert "Одобрить" in taken.text or "Заявок пока нет" in taken.text
        assert "Стать админом" not in taken.text


def test_delete_promo_keeps_guest_progress(client) -> None:
    client.post("/login/demo", data={"role": "business"})
    page = client.get("/biz/promos")
    program_id = page.text.split("/biz/promos/", 1)[1].split("/", 1)[0]
    made = client.post(
        "/biz/earn",
        data={"program_id": program_id, "items": "Кофе", "qty": 1, "amount_rub": 180, "place": "Касса"},
    )
    payload = made.text.split('data-code="', 1)[1].split('"', 1)[0]
    client.post("/login/demo", data={"role": "client"})
    assert client.post("/app/scan", json={"code": payload}).json()["ok"] is True
    me = client.get("/me")
    assert "Моя точка" in me.text
    client.post("/login/demo", data={"role": "business"})
    deleted = client.post(f"/biz/promos/{program_id}/delete", follow_redirects=True)
    assert deleted.status_code == 200
    assert "Прогресс гостей сохранён" in deleted.text or "Снята" in deleted.text
    client.post("/login/demo", data={"role": "client"})
    card = client.get("/me")
    assert "Моя точка" in card.text
    assert "до подарка" in card.text


def test_verify_business_inn(client) -> None:
    client.post("/login/demo", data={"role": "business"})
    saved = client.post(
        "/biz/setup",
        data={
            "name": "Моя точка",
            "city": "Москва",
            "address": "Ленина, 1",
            "inn": "7707083893",
            "director_name": "Иванов Иван Иванович",
        },
        follow_redirects=True,
    )
    assert saved.status_code == 200
    assert "7707083893" in saved.text
    assert "Иванов Иван Иванович" in saved.text
