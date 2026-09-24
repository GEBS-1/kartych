"""Upload Картыч to the VPS and start HTTPS + webhook."""

from __future__ import annotations

import io
import json
import posixpath
import sys
import time
import zipfile
from pathlib import Path

import paramiko
from dotenv import dotenv_values

ROOT = Path(__file__).resolve().parents[1]
REMOTE = "/opt/cupcard"
PUBLIC_HOST = "195.19.7.201.sslip.io"
SKIP = {".venv", ".git", ".pytest_cache", "__pycache__", ".mypy_cache", "htmlcov"}
SKIP_SUFFIX = {".pyc"}


def load_env() -> dict[str, str]:
    env = {k: (v or "") for k, v in dotenv_values(ROOT / ".env").items()}
    if not env.get("VPS_HOST") or not env.get("VPS_PASSWORD"):
        raise SystemExit("VPS_HOST / VPS_PASSWORD empty")
    if not env.get("MAX_BOT_TOKEN"):
        raise SystemExit("MAX_BOT_TOKEN empty")
    return env


def connect(env: dict[str, str]) -> paramiko.SSHClient:
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    client.connect(
        env["VPS_HOST"],
        port=int(env.get("VPS_PORT") or 22),
        username=env.get("VPS_USER") or "root",
        password=env["VPS_PASSWORD"],
        timeout=25,
        allow_agent=False,
        look_for_keys=False,
    )
    return client


def run(client: paramiko.SSHClient, command: str, timeout: int = 180) -> str:
    preview = "[redacted]" if "token=" in command or "MAX_BOT_TOKEN" in command else command[:160]
    print(f"$ {preview}")
    stdin, stdout, stderr = client.exec_command(command, timeout=timeout)
    out = stdout.read().decode("utf-8", "replace")
    err = stderr.read().decode("utf-8", "replace")
    code = stdout.channel.recv_exit_status()
    if out.strip():
        print(out[-4000:])
    if err.strip():
        print(err[-2000:])
    if code != 0:
        raise RuntimeError(f"remote exit {code}: {command}")
    return out


def make_zip() -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for path in ROOT.rglob("*"):
            if not path.is_file():
                continue
            rel = path.relative_to(ROOT)
            parts = set(rel.parts)
            if parts & SKIP or any(part.startswith(".") and part not in {".env.example"} for part in rel.parts[:-1]):
                continue
            if path.suffix in SKIP_SUFFIX:
                continue
            if rel.parts[0] in SKIP:
                continue
            if rel.name == ".env":
                continue
            zf.write(path, rel.as_posix())
    return buf.getvalue()


def server_env(local: dict[str, str], public_base_url: str) -> str:
    return "\n".join(
        [
            f"MAX_BOT_TOKEN={local['MAX_BOT_TOKEN']}",
            f"MAX_BOT_USERNAME={local.get('MAX_BOT_USERNAME') or 't136_hakaton_max_bot'}",
            "MAX_API_BASE=https://platform-api2.max.ru",
            f"PUBLIC_BASE_URL={public_base_url}",
            "WEBHOOK_PATH=/webhook",
            "WEBHOOK_SECRET=CupCardStage1Secret",
            "APP_ENV=production",
            "APP_HOST=127.0.0.1",
            "APP_PORT=8000",
            "LOG_LEVEL=INFO",
            "DATABASE_URL=sqlite+aiosqlite:////opt/cupcard/data/cupcard.db",
            "REDIS_URL=",
            "WATCHDOG_INTERVAL_SECONDS=60",
            "SUBSCRIBE_ON_STARTUP=true",
            "UPDATE_TYPES=bot_started,message_created,message_callback,bot_stopped",
            f"PUBLIC_HOST={PUBLIC_HOST}",
            "MAX_SSL_VERIFY=true",
            "",
        ]
    )


def sftp_put(client: paramiko.SSHClient, data: bytes, dest: str) -> None:
    sftp = client.open_sftp()
    try:
        with sftp.file(dest, "wb") as fh:
            fh.write(data)
    finally:
        sftp.close()


def install_caddy(client: paramiko.SSHClient) -> None:
    run(
        client,
        "if [ ! -x /usr/local/bin/caddy ]; then "
        "curl -fsSL -o /tmp/caddy.tgz https://github.com/caddyserver/caddy/releases/download/v2.10.0/caddy_2.10.0_linux_amd64.tar.gz "
        "&& tar -xzf /tmp/caddy.tgz -C /tmp caddy && mv /tmp/caddy /usr/local/bin/caddy && chmod +x /usr/local/bin/caddy; fi; "
        "caddy version",
        timeout=120,
    )


def wait_http(client: paramiko.SSHClient, url: str, tries: int = 20) -> str:
    last = ""
    for _ in range(tries):
        stdin, stdout, stderr = client.exec_command(
            f"curl -ksS -m 15 -o /tmp/cupcard_health.json -w '%{{http_code}}' {url} || true",
            timeout=25,
        )
        code = stdout.read().decode().strip()
        last = run(client, "cat /tmp/cupcard_health.json 2>/dev/null || true")
        print("health", code, last[:300])
        if code.startswith("2"):
            return last
        time.sleep(3)
    raise RuntimeError(f"health not ready: {url} {last}")


def subscribe(client: paramiko.SSHClient, env: dict[str, str], webhook_url: str) -> str:
    payload = json.dumps(
        {
            "url": webhook_url,
            "update_types": ["bot_started", "message_created", "message_callback", "bot_stopped"],
            "secret": "CupCardStage1Secret",
        }
    )
    cmd = (
        "python3 - <<'PY'\n"
        "import json,urllib.request,ssl\n"
        f"token={env['MAX_BOT_TOKEN']!r}\n"
        f"body={payload!r}\n"
        "ctx=ssl._create_unverified_context()\n"
        "req=urllib.request.Request('https://platform-api2.max.ru/subscriptions', data=body.encode(), method='POST',\n"
        "  headers={'Authorization': token, 'Content-Type': 'application/json'})\n"
        "try:\n"
        "  with urllib.request.urlopen(req, context=ctx, timeout=20) as r:\n"
        "    print(r.status); print(r.read().decode())\n"
        "except Exception as e:\n"
        "  if hasattr(e,'read'): print(e.read().decode())\n"
        "  print(type(e).__name__, e)\n"
        "PY"
    )
    return run(client, cmd)


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    env = load_env()
    public_base = f"https://{PUBLIC_HOST}"
    print("zipping...")
    archive = make_zip()
    print("zip bytes", len(archive))
    client = connect(env)
    try:
        run(client, "mkdir -p /opt/cupcard /opt/cupcard/data /tmp")
        sftp_put(client, archive, "/tmp/cupcard.zip")
        sftp_put(client, server_env(env, public_base).encode("utf-8"), "/tmp/cupcard.env")
        run(
            client,
            "python3 - <<'PY'\n"
            "import zipfile, pathlib, shutil\n"
            "root=pathlib.Path('/opt/cupcard')\n"
            "for name in ['app','deploy','alembic','tests']:\n"
            "    p=root/name\n"
            "    if p.exists(): shutil.rmtree(p, ignore_errors=True)\n"
            "with zipfile.ZipFile('/tmp/cupcard.zip') as z:\n"
            "    z.extractall('/opt/cupcard')\n"
            "    print('extracted', len(z.namelist()))\n"
            "PY",
        )
        run(client, f"mv /tmp/cupcard.env {REMOTE}/.env; mkdir -p {REMOTE}/data")
        run(
            client,
            f"cd {REMOTE} && python3 -m venv .venv && .venv/bin/pip install -q -U pip && .venv/bin/pip install -q -r requirements.prod.txt && .venv/bin/python -c 'import app.main; print(\"import-ok\")'",
            timeout=300,
        )
        run(client, "ufw allow 443/tcp || true; ufw allow 22/tcp || true")
        install_caddy(client)
        run(
            client,
            "cp /opt/cupcard/deploy/cupcard.service /etc/systemd/system/cupcard.service; "
            "cp /opt/cupcard/deploy/cupcard-caddy.service /etc/systemd/system/cupcard-caddy.service; "
            "systemctl daemon-reload; "
            "systemctl enable --now cupcard.service; "
            "systemctl restart cupcard.service; "
            "sleep 2; systemctl is-active cupcard.service; "
            "curl -fsS http://127.0.0.1:8000/health || true",
        )
        run(client, "systemctl enable --now cupcard-caddy.service; sleep 4; systemctl is-active cupcard-caddy.service || true")
        public_base = f"https://{PUBLIC_HOST}"
        try:
            health = wait_http(client, f"{public_base}/health")
        except Exception as exc:
            print("sslip/caddy failed:", exc)
            run(
                client,
                "cp /opt/cupcard/deploy/cupcard-tunnel.service /etc/systemd/system/cupcard-tunnel.service; "
                "systemctl daemon-reload; "
                "systemctl enable --now cupcard-tunnel.service; "
                "sleep 8; "
                "grep -oE 'https://[a-zA-Z0-9.-]+\\.trycloudflare.com' /var/log/cloudflared-cupcard.log | tail -1",
            )
            tunnel = run(
                client,
                "grep -oE 'https://[a-zA-Z0-9.-]+\\.trycloudflare.com' /var/log/cloudflared-cupcard.log | tail -1",
            ).strip()
            if not tunnel:
                raise RuntimeError("no trycloudflare url") from exc
            public_base = tunnel
            sftp_put(client, server_env(env, public_base).encode("utf-8"), f"{REMOTE}/.env")
            run(client, "systemctl restart cupcard.service; sleep 3")
            health = wait_http(client, f"{public_base}/health")
        print("PUBLIC", public_base)
        print("HEALTH", health)
        print("SUBSCRIBE", subscribe(client, env, f"{public_base}/webhook"))
        run(client, "systemctl is-active cupcard.service")
        run(client, "journalctl -u cupcard.service -n 20 --no-pager || true")
    finally:
        client.close()


if __name__ == "__main__":
    main()
