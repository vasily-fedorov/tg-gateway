# AGENTS.md — хаб tg-gateway

Сервис-бот: маршруты, карточки владельца, состояние задач, аудит. Плагин агента живёт в
`../hermes-tg-gateway` — здесь его кода нет.

## Структура

| Файл | Назначение |
|---|---|
| `hub/app.py` | диспетчер апдейтов, JSON-RPC методы, карточки, команды владельца. |
| `hub/db.py` | SQLite-схема и DAO (bots, routes, tasks, events, links, invites, peer_health, inbox_msgs, owners). |
| `hub/rpc.py` | A2A-объекты (Task/TaskStatus/Part/AgentCard), коды ошибок, маппинг состояний. |
| `hub/policy.py` | лимиты, hops, фильтры секретов, TTL, отпечаток содержимого. |
| `hub/protocol.py` | текст протокола для агента (`/protocol`, RPC `agent/protocol`). |
| `hub/tg.py` | Bot API (long polling, send/edit/callback, прокси). |
| `docs/protocol.md` | **контракт** с плагином. Меняется — версия протокола меняется. |
| `deploy/` | systemd user-unit + `install.sh`. |

## Проверка перед коммитом

```bash
python3 tests/test_hub.py
python3 -c "import ast; ast.parse(open('hub/app.py').read())"
```

## Инварианты

1. **Кнопка = подтверждение.** Доставка только из `on_callback` после клика владельца задачи.
2. **Новый исходящий спрашивает владельца**, продолжение по тому же `ctx` — по политике маршрута.
3. **Чужой клик невозможен**: `task.owner_tg == uid`, иначе `card_unauthorized`.
4. **Глубина**: `hops ≤ 3`; 4-е сообщение не доставляется (`failed(hop_limit)`).
5. **Дедуп**: `messageId` + отпечаток `sha256(src:dst:text+attachments)`, окно 300 с.
6. **TTL конверта** 300 с по `metadata.sent_at`.
7. **Секреты не пересылаются**: `policy.scan_text` на любом исходящем.
8. **Аудит** каждого решения; hash/длины, не текст.
9. **Совместимость**: меняешь поля в `docs/protocol.md` → поднимай `PROTOCOL_VERSION`
   (в `hub/rpc.py`) и синхронно обновляй плагин в `hermes-tg-gateway`.

## Грабли Telegram

- General (топик 1) не имеет `message_thread_id`; в ЛС thread не передаётся вовсе.
- `file_id` привязан к боту — переслать файл «как есть» нельзя (поэтому `file` = ссылка).
- Bot-to-bot DM работает только если у обоих включён B2B-режим **и** они состоят в общем чате.
- Лимиты Bot API: отправка 50 МБ, скачивание `getFile` 20 МБ.

## Стиль

- Только stdlib (`urllib`, `sqlite3`, `json`). Новых зависимостей не добавлять.
- Русские строки в UI/аудите, идентификаторы — английские.
- Исключения в обработчике апдейта не пробрасывать: `db.event("hub_error", …)`.
