import re
from datetime import UTC, datetime, timedelta

from sqlalchemy import select

from app.db.models import Business, Customer, LoyaltyProgram, Receipt
from app.web.insights import analytics, league


def sign_in(client, role):
    return client.post("/login/demo", data={"role": role})


def make_qr(client, program_id=""):
    response = client.post(
        "/biz/earn", data={"items": "Кофе", "qty": 1, "amount_rub": 180, "program_id": program_id}
    )
    assert response.status_code == 200
    return re.search(r'data-code="([^"]+)"', response.text)[1]


def test_new_pages_and_navigation(client):
    sign_in(client, "client")
    for path, heading in [
        ("/me", "Мои карты"),
        ("/me/league", "Лига недели"),
        ("/me/shops", "Рядом"),
        ("/me/scan", "Сканер QR"),
        ("/settings", "Мой профиль"),
    ]:
        response = client.get(path)
        assert response.status_code == 200
        assert heading in response.text
        assert "/static/css/design.css" in response.text
    shops = client.get("/me/shops")
    assert "/static/vendor/leaflet/leaflet.min.js" in shops.text
    assert "cartocdn" not in shops.text
    js = client.get("/static/js/app.js").text
    assert "arcgisonline" not in js
    assert "attributionControl:false" in js.replace(" ", "")
    assert "prefix:false" in js.replace(" ", "")
    assert "cup-max-login-opened" in js
    assert "location.assign" in js
    leaflet = client.get("/static/vendor/leaflet/leaflet.min.js").text
    assert "leaflet-attribution-flag" not in leaflet
    assert "#4C7BE1" not in leaflet
    assert "prefix:false" in leaflet
    sign_in(client, "business")
    for path in [
        "/biz",
        "/biz?period=month",
        "/biz/earn",
        "/biz/promos",
        "/biz/games",
        "/biz/clients",
        "/settings",
        "/biz/staff",
        "/biz/scan",
    ]:
        assert client.get(path).status_code == 200


def test_game_reward_once_and_purchase_replay(client, app):
    sign_in(client, "business")
    assert (
        client.post(
            "/biz/games", data={"title": "Первый визит", "goal": 1, "bonus": 80}
        ).status_code
        == 200
    )
    game_id = re.search(r"/biz/games/([^/]+)/toggle", client.get("/biz/games").text)[1]
    code = make_qr(client)
    sign_in(client, "client")
    assert client.post(f"/me/games/{game_id}/claim").status_code == 400
    assert client.post("/app/scan", json={"code": code}).json()["ok"]
    assert not client.post("/app/scan", json={"code": code}).json()["ok"]
    assert "10 XP" in client.get("/me/league").text
    assert client.post(f"/me/games/{game_id}/claim").status_code == 200
    assert client.post(f"/me/games/{game_id}/claim").status_code == 200

    async def check():
        async with app.state.session_factory() as session:
            customer = await session.scalar(select(Customer).where(Customer.max_user_id == -11))
            assert customer.bonus == 80
            return customer.business_id

    shop_id = client.portal.call(check)
    assert "80" in client.get(f"/me/cards/{shop_id}").text
    redeem = client.get(f"/me/redeem/{shop_id}")
    redeem_code = re.search(r'data-code="([^"]+)"', redeem.text)[1]
    assert not client.post("/app/scan", json={"code": redeem_code}).json()["ok"]
    sign_in(client, "business")
    assert client.post("/app/scan", json={"code": redeem_code}).json()["ok"]
    assert not client.post("/app/scan", json={"code": redeem_code}).json()["ok"]
    assert "Списано баллов" in client.get("/biz").text

    async def balance():
        async with app.state.session_factory() as session:
            customer = await session.scalar(select(Customer).where(Customer.max_user_id == -11))
            assert customer.bonus == 30
            shop = await session.get(Business, shop_id)
            stats = await analytics(session, shop, "week")
            assert stats["visits"] == 1
            assert stats["redeemed"] == 50

    client.portal.call(balance)


def test_bonus_cycles_and_program_ownership(client, app):
    sign_in(client, "business")
    client.post(
        "/biz/promo",
        data={
            "title": "Каждые два визита",
            "kind": "visits",
            "goal": 2,
            "bonus": 40,
            "reward": "Баллы",
        },
    )

    async def ids():
        async with app.state.session_factory() as session:
            own = await session.scalar(
                select(LoyaltyProgram).where(LoyaltyProgram.title == "Каждые два визита")
            )
            foreign = await session.scalar(
                select(LoyaltyProgram).where(LoyaltyProgram.business_id != own.business_id)
            )
            return own.id, foreign.id

    own, foreign = client.portal.call(ids)
    assert client.post("/biz/earn", data={"program_id": foreign}).status_code == 422
    assert client.post(f"/biz/promos/{foreign}/toggle").status_code == 404
    codes = [make_qr(client, own) for _ in range(4)]
    sign_in(client, "client")

    async def bonus():
        async with app.state.session_factory() as session:
            return (await session.scalar(select(Customer).where(Customer.max_user_id == -11))).bonus

    for code, expected in zip(codes, [0, 40, 40, 80], strict=True):
        assert client.post("/app/scan", json={"code": code}).json()["ok"]
        assert client.portal.call(bonus) == expected


def test_map_profile_validation_and_zero_balance(client, app):
    sign_in(client, "business")
    assert (
        client.post(
            "/biz/setup", data={"name": "Точка", "latitude": "91", "longitude": "37"}
        ).status_code
        == 422
    )
    assert (
        client.post(
            "/biz/setup",
            data={
                "name": "Точка",
                "city": "Москва",
                "address": "Ленина, 10",
                "latitude": "55.75",
                "longitude": "37.61",
            },
        ).status_code
        == 200
    )
    assert "55.75" in client.get("/me/shops").text

    async def shop_id():
        async with app.state.session_factory() as session:
            return (
                await session.scalar(select(Business).where(Business.owner_max_user_id == -22))
            ).id

    sid = client.portal.call(shop_id)
    sign_in(client, "client")
    assert client.get(f"/me/redeem/{sid}").status_code == 400


def test_stats_periods_and_empty_issued_qr(client, app):
    sign_in(client, "business")
    make_qr(client)

    async def check():
        async with app.state.session_factory() as session:
            shop = await session.scalar(select(Business).where(Business.owner_max_user_id == -22))
            assert (await analytics(session, shop, "week"))["visits"] == 0
            customer = Customer(business_id=shop.id, max_user_id=999, display_name="Гость")
            session.add(customer)
            await session.flush()
            for days in [0, 1, 12]:
                session.add(
                    Receipt(
                        business_id=shop.id,
                        customer_id=customer.id,
                        kind="earn",
                        created_at=datetime.now(UTC) - timedelta(days=days),
                    )
                )
            await session.flush()
            assert (await analytics(session, shop, "week"))["visits"] == 2
            assert (await analytics(session, shop, "month"))["visits"] == 3
            assert (await league(session, 999, "today"))["mine"]["xp"] == 10

    client.portal.call(check)


def test_demo_unavailable_in_production(client, app):
    app.state.settings.app_env = "production"
    assert client.post("/login/demo", data={"role": "business"}).status_code == 404
    assert client.post("/app/auth", json={"demo": "client"}).status_code == 401


def test_physical_reward_requires_cashier_and_cannot_repeat(client, app):
    sign_in(client, "business")
    client.post("/biz/promo", data={"title": "Подарок за визит", "goal": 1, "reward": "Десерт"})

    async def ids():
        async with app.state.session_factory() as session:
            program = await session.scalar(
                select(LoyaltyProgram).where(LoyaltyProgram.title == "Подарок за визит")
            )
            return program.business_id, program.id

    sid, pid = client.portal.call(ids)
    code = make_qr(client, pid)
    sign_in(client, "client")
    assert client.post(f"/me/rewards/{sid}/{pid}").status_code == 404
    assert client.post("/app/scan", json={"code": code}).json()["ok"]
    reward_codes = []
    for _ in range(2):
        page = client.post(f"/me/rewards/{sid}/{pid}")
        assert page.status_code == 200
        reward_codes.append(re.search(r'data-code="([^"]+)"', page.text)[1])
    assert not client.post("/app/scan", json={"code": reward_codes[0]}).json()["ok"]
    sign_in(client, "business")
    assert client.post("/app/scan", json={"code": reward_codes[0]}).json()["ok"]
    assert not client.post("/app/scan", json={"code": reward_codes[1]}).json()["ok"]
    assert not client.post("/app/scan", json={"code": reward_codes[0]}).json()["ok"]


def test_negative_receipt_rejected(client):
    sign_in(client, "business")
    assert client.post("/biz/earn", data={"amount_rub": -1}).status_code == 422
    assert client.post("/biz/earn", data={"qty": 0}).status_code == 422


def test_catalog_game_and_rename_keeps_one_point(client, app):
    sign_in(client, "business")
    promo = client.get("/biz/promos")
    assert "Приветственный бонус" in promo.text
    assert "Колесо удачи" in promo.text
    assert "Приведи друга" in promo.text
    assert client.post("/biz/games/catalog", data={"slug": "welcome"}).status_code == 200
    assert "Приветственный бонус" in client.get("/biz/promos").text

    async def owned():
        async with app.state.session_factory() as session:
            rows = (
                await session.scalars(select(Business).where(Business.owner_max_user_id == -22))
            ).all()
            return [(row.id, row.name, row.parent_id) for row in rows]

    before = client.portal.call(owned)
    assert len(before) == 1
    assert (
        client.post(
            "/biz/setup",
            data={"name": "Северная", "city": "Москва", "org_name": "Сеть Зёрна"},
        ).status_code
        == 200
    )
    after_rename = client.portal.call(owned)
    assert len(after_rename) == 1
    assert after_rename[0][1] == "Северная"
    assert (
        client.post(
            "/biz/point",
            data={"name": "Филиал", "city": "Казань", "address": "Баумана, 1"},
        ).status_code
        == 200
    )
    after_add = client.portal.call(owned)
    assert len(after_add) == 2
    assert {row[1] for row in after_add} == {"Северная", "Филиал"}
    assert (
        client.post(
            "/biz/setup",
            data={"name": "Северная", "city": "Москва", "org_name": "Сеть Зёрна"},
        ).status_code
        == 200
    )
    assert len(client.portal.call(owned)) == 2


def test_bot_qr_start_opens_scan_for_business(client):
    sign_in(client, "business")
    response = client.get("/app?start=qr", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/biz/scan"
    home = client.get("/app?start=home", follow_redirects=False)
    assert home.headers["location"] == "/biz"
    smart = client.get("/qr", follow_redirects=False)
    assert smart.headers["location"] == "/biz/scan"
