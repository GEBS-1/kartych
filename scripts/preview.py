"""Isolated local demo; never reads .env or connects to the live bot/database.

Run from the repository root: .venv/Scripts/python.exe -m scripts.preview
"""

import uvicorn

from app.config import Settings
from app.main import create_app

if __name__ == "__main__":
    uvicorn.run(
        create_app(
            Settings(
                _env_file=None,
                app_env="test",
                max_bot_token="preview-token",
                max_bot_username="cupcard_bot",
                webhook_secret="local-preview-only",
                public_base_url="http://127.0.0.1:8765",
                subscribe_on_startup=False,
            )
        ),
        host="127.0.0.1",
        port=8765,
    )
