from faker import Faker


def test_health(client) -> None:
    response = client.get("/health")
    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    assert body["bot"]["username"] == "cupcard_bot"


def test_home_and_miniapp(client) -> None:
    fake = Faker("ru_RU")
    assert fake.name()
    assert client.get("/").status_code == 200
    assert client.get("/app").status_code == 200
    assert client.get("/app/balance").status_code == 200
    assert client.get("/dashboard").status_code == 200
