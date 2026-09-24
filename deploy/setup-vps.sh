#!/usr/bin/env bash
set -euo pipefail

# Каркас деплоя на Ubuntu VPS: Docker + Compose.
# Запускать на сервере из каталога репозитория.

if [[ "${EUID}" -ne 0 ]]; then
  echo "Запусти от root или через sudo."
  exit 1
fi

export DEBIAN_FRONTEND=noninteractive
apt-get update
apt-get install -y ca-certificates curl git ufw

if ! command -v docker >/dev/null 2>&1; then
  curl -fsSL https://get.docker.com | sh
fi

ufw allow OpenSSH
ufw allow 80/tcp
ufw allow 443/tcp
ufw --force enable

if [[ ! -f .env ]]; then
  echo "Положи .env рядом со скриптом и запусти снова."
  exit 1
fi

docker compose -f docker-compose.prod.yml up -d --build
docker compose -f docker-compose.prod.yml exec -T app alembic upgrade head
echo "Готово. Проверь https://${PUBLIC_HOST:-your-domain.ru}/health"
