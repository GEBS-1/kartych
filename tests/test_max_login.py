from pathlib import Path

from app.web.max_login import LoginTickets, tickets_path_for


def test_tickets_path_next_to_sqlite() -> None:
    path = tickets_path_for("sqlite+aiosqlite:////opt/cupcard/data/cupcard.db")
    assert path is not None
    assert path.name == "login_tickets.json"
    assert path.parent.as_posix().endswith("/opt/cupcard/data")
    assert tickets_path_for("sqlite+aiosqlite://") is None
    assert tickets_path_for("postgresql+asyncpg://cupcard:cupcard@localhost/cupcard") is None


def test_login_tickets_survive_reload(tmp_path: Path) -> None:
    store = tmp_path / "login_tickets.json"
    first = LoginTickets(store)
    token = first.create(role="client")
    assert first.complete(token, 4242) is True
    assert first.complete(token, 4242) is True
    again = LoginTickets(store)
    assert again.status(token) == "ok"
    item = again.consume(token)
    assert item is not None
    assert item["user_id"] == 4242
    assert LoginTickets(store).status(token) == "used"
    replay = LoginTickets(store).consume(token)
    assert replay is not None
    assert replay["user_id"] == 4242
