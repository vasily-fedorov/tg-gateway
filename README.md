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

## A2A по HTTP (прокси для стандартных клиентов)

Сам хаб живёт в Telegram и URL не имеет. Чтобы к нему мог подключиться обычный
A2A-клиент (Hermes `a2a_call`, Google ADK, LangChain), хаб поднимает локальный
HTTP-фронт — тот же код, тот же процесс, та же БД:

```
A2A client ──HTTP/JSON-RPC──▶ hub :18765 ──карточка──▶ владелец ──клик──▶ бот (Telegram)
```

Включается переменными:

```
TG_GATEWAY_A2A_PORT=18765          # 0 = выключено
TG_GATEWAY_A2A_HOST=127.0.0.1      # наружу — только через nginx + TLS
TG_GATEWAY_A2A_TOKEN=<bearer>      # без него фронт отвечает 401
TG_GATEWAY_A2A_WAIT=25             # сколько ждать клика владельца внутри message/send
TG_GATEWAY_A2A_PUBLIC_URL=https://a2a.example.com   # что писать в карточке (за nginx)
```

Что поддерживается:

- `GET /.well-known/agent-card.json` (и алиас `agent.json`) — карточка хаба, боты = skills;
- `GET /bot/<alias>/.well-known/agent-card.json` — карточка конкретного бота: у неё
  `supportedInterfaces[].url = <public>/bot/<alias>`, поэтому клиент, знающий только URL,
  сразу попадает в нужного бота (клиент Hermes не умеет передавать `params.to`);
- `POST /` JSON-RPC: `message/send`, `tasks/get`, `tasks/list`, `tasks/cancel`,
  `agent/card`, `agent/protocol`;
- `GET /health`.

Методы принимаются и в v1.0-написании: `SendMessage`, `GetTask`, `CancelTask`,
`ListTasks` (клиент Hermes шлёт именно `SendMessage`).

Честные ограничения: `message/send` **всегда асинхронный** — владелец подтверждает
карточку, поэтому клиент получает Task в состоянии `auth-required` и дальше поллит
`tasks/get`. Стриминга и push-вебхуков нет (`capabilities.streaming/pushNotifications =
false`). Один токен = один владелец (его боты и маршруты).

Пример:

```bash
curl -s -H "Authorization: Bearer $TOKEN" \
  http://127.0.0.1:18765/.well-known/agent-card.json
curl -s -X POST -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d '{"jsonrpc":"2.0","id":1,"method":"message/send",
       "params":{"to":"aa","message":{"parts":[{"kind":"text","text":"сколько места на /?"}]}}}' \
  http://127.0.0.1:18765/
```

## Тесты

```bash
python3 tests/test_hub.py
```

## Аудит

SQLite-таблица `events`: `relay_parked`, `card_approved`, `delivered`, `reply_parked`,
`task_skipped`, `ttl_rejected`, `rpc_dedup`, `hop_limit`, `peer_blocked`,
`card_unauthorized`, `owner_log`, `hub_error`. Пишутся hash/длины, не текст.
