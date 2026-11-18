#!/bin/bash
# Установка/обновление хаба как user-unit. Токен — в ~/tg-gateway/.env (0600).
set -euo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"
DATA="$HOME/tg-gateway"
UNIT_DIR="$HOME/.config/systemd/user"

mkdir -p "$DATA" "$UNIT_DIR"
chmod 700 "$DATA"

if [ ! -f "$DATA/.env" ]; then
  echo "создайте $DATA/.env: TG_GATEWAY_TOKEN, TG_GATEWAY_OWNERS, TG_GATEWAY_OPEN=0, TG_GATEWAY_DB" >&2
  exit 1
fi
chmod 600 "$DATA/.env"

sed "s|WorkingDirectory=.*|WorkingDirectory=$REPO|; s|EnvironmentFile=.*|EnvironmentFile=$DATA/.env|" \
  "$REPO/deploy/tg-gateway-hub.service" > "$UNIT_DIR/tg-gateway-hub.service"

systemctl --user daemon-reload
systemctl --user enable --now tg-gateway-hub.service
sleep 2
systemctl --user --no-pager status tg-gateway-hub.service | head -12
echo "лог: journalctl --user -u tg-gateway-hub -f"
