def test_miniapp_shell(client) -> None:
    response = client.get("/app")
    assert response.status_code == 200
    assert "cabinet" in response.text
    assert "max-allow" in response.text
    assert "max-browser" in response.text


def test_qr_png(client) -> None:
    response = client.get("/qr/demo-shop.png")
    assert response.status_code == 200
    assert response.headers["content-type"] == "image/png"
    assert response.content[:8] == b"\x89PNG\r\n\x1a\n"


def test_demo_client_sees_shops(client) -> None:
    auth = client.post("/app/auth", json={"demo": "client"})
    assert auth.status_code == 200
    assert auth.json()["ok"] is True
    page = client.get("/app/client")
    assert page.status_code == 200
    assert "Точка на Ленина" in page.text
    assert "Камера" in page.text


def test_business_qr_and_client_scan(client) -> None:
    assert client.post("/app/auth", json={"demo": "business"}).json()["ok"] is True
    qr = client.get("/app/business/qr")
    assert qr.status_code == 200
    code = qr.text.split('data-code="', 1)[1].split('"', 1)[0]
    assert code.startswith("cupcard:")

    assert client.post("/app/auth", json={"demo": "client"}).json()["ok"] is True
    scanned = client.post("/app/scan", json={"code": code})
    body = scanned.json()
    assert scanned.status_code == 200
    assert body["ok"] is True
    assert body["visits"] == 1
    cards = client.get("/app/client")
    assert "Моя точка" in cards.text
