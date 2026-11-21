# AGENTS.md — хаб tg-gateway

Сервис-бот: маршруты, карточки владельца, состояние задач, аудит. Плагин агента живёт в
[`hermes-tg-gateway`](https://github.com/vasily-fedorov/hermes-tg-gateway) — здесь его кода нет.

## Структура

| Файл | Назначение |
|---|---|
| `hub/app.py` | диспетчер апдейтов, JSON-RPC методы, карточки, команды владельца. |
| `hub/db.py` | SQLite-схема и DAO (owners, bots, pair_codes, routes, tasks, task_messages, events, inbox_msgs, peer_health). |
| `hub/rpc.py` | A2A-объекты (Task/TaskStatus/Part/AgentCard), коды ошибок, маппинг состояний. |
| `hub/policy.py` | лимиты, hops, фильтры секретов, TTL, отпечаток содержимого. |
| `hub/protocol.py` | текст протокола для агента (`/protocol`, RPC `agent/protocol`). |
| `hub/tg.py` | Bot API (long polling, send/edit/callback, прокси). |
| `docs/protocol.md` | **контракт** (Telegram, асинхронный A2A). Меняется — версия протокола меняется. |
| `tests/test_hub.py` | единственный набор тестов хаба (группы `res[...]`). |
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
4. **Глубина**: `hops ≤ 6`; ответ на задачу глубину не увеличивает, ответ на ответ новую
   задачу не создаёт (петля гасится в `on_reply`, `deliver` закрывает reply-задачу сразу).
5. **Дедуп**: `messageId` + отпечаток `sha256(src:dst:text+attachments)`, окно 300 с.
6. **TTL конверта** 300 с по `metadata.sent_at`.
7. **Секреты не пересылаются**: `policy.scan_text` на любом исходящем.
8. **Аудит** каждого решения; hash/длины, не текст.
9. **Совместимость**: меняешь поля в `docs/protocol.md` → поднимай `PROTOCOL_VERSION`
   (в `hub/rpc.py`) и синхронно обновляй плагин в `hermes-tg-gateway`.
10. **Асинхронность**: `message/send` возвращает Task сразу (`auth-required` при `ask`,
    `working` при `auto`); результат — только `tasks/get`/`tasks/list`. `tasks/update` —
    внутренний канал исполнителя и разбирается по `status.state` (working = прогресс и
    хартбит, input-required = вопрос, completed = финал с artifact). Апдейты по
    терминальной задаче игнорируются.
11. **Политика подтверждения**: только маршрут `(src → dst)` — `ask` по умолчанию,
    `auto` отключает карточку. Внешних клиентов и межвладельческих связей нет.
11. **`artifacts` — результат**: ответ агента лежит в `artifacts[0]`, вход и ответ —
    в `history` (так его читает клиент Hermes).
12. **Discovery закрыт**: аноним получает 401; с токеном клиента видны только его боты;
    `TG_GATEWAY_A2A_PUBLIC_CARD=1` не раскрывает список агентов.
13. **Изоляция клиентов**: `tasks.client_id` — `GetTask`/`CancelTask`/`ListTasks` чужих
    задач не видят; отзыв и ротация токена действуют сразу (401).

## Грабли Telegram

- General (топик 1) не имеет `message_thread_id`; в ЛС thread не передаётся вовсе.
- `file_id` привязан к боту — переслать файл «как есть» нельзя (поэтому `file` = ссылка).
- Bot-to-bot DM работает только если у обоих включён B2B-режим **и** они состоят в общем чате.
- Лимиты Bot API: отправка 50 МБ, скачивание `getFile` 20 МБ.

## Стиль

- Только stdlib (`urllib`, `sqlite3`, `json`). Новых зависимостей не добавлять.
- Русские строки в UI/аудите, идентификаторы — английские.
- Исключения в обработчике апдейта не пробрасывать: `db.event("hub_error", …)`.
