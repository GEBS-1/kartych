def test_webhook_rejects_bad_secret(client) -> None:
    response = client.post("/webhook", json={"update_type": "bot_started"})
    assert response.status_code == 403


def test_bot_started_echo(client, app) -> None:
    payload = {
        "update_type": "bot_started",
        "timestamp": 1,
        "chat_id": 100,
        "user": {"user_id": 42, "name": "Иван", "username": "ivan"},
    }
    response = client.post(
        "/webhook",
        json=payload,
        headers={"X-Max-Bot-Api-Secret": "secret123"},
    )
    assert response.status_code == 200
    assert response.json()["accepted"] == 1
    app.state.max_client.send_message.assert_awaited()
    kwargs = app.state.max_client.send_message.await_args.kwargs
    assert kwargs["user_id"] == 42
    assert "Картыч" in kwargs["text"]
    assert "http" not in kwargs["text"].lower()
    assert kwargs["attachments"]
    buttons = kwargs["attachments"][0]["payload"]["buttons"]
    assert buttons[0][0]["type"] == "callback"
    assert buttons[0][0]["text"] == "Показать QR"
    assert buttons[0][0]["payload"] == "showqr"
    assert buttons[1][0]["type"] == "callback"
    assert buttons[1][0]["text"] == "Открыть кабинет"
    assert buttons[1][0]["payload"] == "openapp"
    assert buttons[2][0]["text"] == "Поддержка"
    assert buttons[2][0]["payload"] == "help"
    assert len(buttons) == 3
    assert all(btn["type"] != "link" for row in buttons for btn in row)


def test_guest_cabinet_callback(client, app) -> None:
    payload = {
        "update_type": "message_callback",
        "timestamp": 1,
        "callback": {
            "callback_id": "cb1",
            "payload": "role:client",
            "user": {"user_id": 42, "name": "Иван"},
        },
    }
    response = client.post(
        "/webhook",
        json=payload,
        headers={"X-Max-Bot-Api-Secret": "secret123"},
    )
    assert response.status_code == 200
    app.state.max_client.answer_callback.assert_awaited()
    app.state.max_client.send_message.assert_awaited()
    text = app.state.max_client.send_message.await_args.kwargs["text"]
    assert "гостя" in text.lower()


def test_scan_callback_opens_camera(client, app) -> None:
    payload = {
        "update_type": "message_callback",
        "timestamp": 1,
        "callback": {
            "callback_id": "cb-scan",
            "payload": "scan",
            "user": {"user_id": 42, "name": "Иван"},
        },
    }
    response = client.post(
        "/webhook",
        json=payload,
        headers={"X-Max-Bot-Api-Secret": "secret123"},
    )
    assert response.status_code == 200
    kwargs = app.state.max_client.send_message.await_args.kwargs
    assert "камер" in kwargs["text"].lower()
    button = kwargs["attachments"][0]["payload"]["buttons"][0][0]
    assert button["type"] == "open_app"
    assert button.get("payload") == "scan"


def test_ping_echo(client, app) -> None:
    payload = {
        "update_type": "message_created",
        "timestamp": 1,
        "message": {
            "sender": {"user_id": 7, "name": "Тест"},
            "recipient": {"chat_id": 7, "user_id": 7},
            "body": {"text": "ping"},
        },
    }
    response = client.post(
        "/webhook",
        json=payload,
        headers={"X-Max-Bot-Api-Secret": "secret123"},
    )
    assert response.status_code == 200
    app.state.max_client.send_message.assert_awaited()
    assert "pong" in app.state.max_client.send_message.await_args.kwargs["text"]
