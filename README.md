# tg-gateway — хаб межагентного обмена

Telegram-бот, который держит маршруты, подтверждения владельца и состояние задач между
агентами. Плагин агента (Hermes) живёт отдельно: `../hermes-tg-gateway`.

```
 агент A ─JSON-RPC─▶ хаб ─карточка─▶ владелец ─клик─▶ хаб ─JSON-RPC─▶ агент B
                     ▲                                              │
                     └──────── tasks/update (completed/skip/ask) ◀──┘
```

Транспорт — **Telegram DM** (бот-хаб ↔ бот-агент), протокол — **JSON-RPC 2.0 / A2A v1.0**.
Спека: [`docs/protocol.md`](docs/protocol.md).

## Что делает

- привязка ботов одноразовым кодом (`/pair` → бот отправляет `/pair <код> <алиас>`);
- маршруты с политикой подтверждений (`ask` — карточка владельцу, `auto` — продолжение без карточки);
- карточки с кнопками в ЛС владельца; без клика ничего не уходит;
- состояния задачи по A2A (`auth-required → working → input-required → completed|failed|rejected|canceled`), `skipped` = «нечего отвечать»;
- лимиты (`max_text`, `per_hour`), глубина цепочки (hops ≤ 3), TTL конверта 300 с, дедуп по `messageId` и по отпечатку содержимого;
- здоровье пиров: 3 неудачные доставки → блокировка на 5 минут;
- мультитенантность: у каждого владельца свои боты, маршруты, задачи, аудит; связи между владельцами — `/share` + `/link` с подтверждением с двух сторон;
- части сообщения: `text`, `data` (JSON), `file` (только ссылка, бинарь не передаётся).

## Команды владельца (в ЛС с ботом-хабом)

```
/pair <алиас>                код привязки бота
/bots                        подключённые боты
/routes                      маршруты и политика
/route <от> <кому> ask|auto  изменить политику ('-' = владелец)
/log [алиас] [N]             последние задачи: пришедшие и отправленные
/task <id>                   детали задачи и её события
/protocol                    протокол в формате для агента
/card [алиас]                A2A-карточка хаба или бота
/describe <алиас> <текст>    что бот умеет (попадёт в карточку)
/share <алиас>               приглашение для связи с чужим владельцем
/link <код> <мой_алиас>      связать своего бота с чужим
/links, /unlink <id>         список и разрыв связей
/cancel <id>, /unpair <алиас>
/help
```

## Запуск

```bash
mkdir -p ~/tg-gateway && chmod 700 ~/tg-gateway
cat > ~/tg-gateway/.env <<'EOF'
TG_GATEWAY_TOKEN=<токен бота-хаба>
TG_GATEWAY_OWNERS=<tg_id владельца>
TG_GATEWAY_OPEN=0          # 1 — разрешить самостоятельную регистрацию владельцев
TG_GATEWAY_DB=/home/<user>/tg-gateway/hub.sqlite3
TG_GATEWAY_PROXY=<http-прокси>  # если до api.telegram.org нужен прокси
EOF
chmod 600 ~/tg-gateway/.env
bash deploy/install.sh     # user-unit tg-gateway-hub
```

Боту в @BotFather нужны: **Bot-to-Bot Communication Mode** и членство в одной группе с
ботами-агентами (Telegram открывает bot-to-bot DM только при общем чате).

## Тесты

```bash
python3 tests/test_hub.py
```

## Аудит

SQLite-таблица `events`: `relay_parked`, `card_approved`, `delivered`, `reply_parked`,
`task_skipped`, `ttl_rejected`, `rpc_dedup`, `hop_limit`, `peer_blocked`,
`card_unauthorized`, `owner_log`, `hub_error`. Пишутся hash/длины, не текст.
