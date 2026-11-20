# tg-gateway protocol v1

Основной транспорт — **Telegram DM** (бот-хаб ↔ бот-агент). Контракт — **JSON-RPC 2.0**
с объектами в духе A2A v1.0. Плагин агента живёт отдельно (`hermes-tg-gateway`).

Дополнительно хаб умеет поднимать **HTTP-фронт** для стандартных A2A-клиентов
(`hub/a2a_http.py`, §8): тот же контракт, но по HTTPS с Bearer-токеном. Telegram при этом
остаётся основным транспортом, а HTTP — опциональный вход.

Чтобы подключить бота с другого хоста через Telegram, достаточно добавить его в общую
супергруппу с хабом (Telegram открывает bot-to-bot DM только при общем чате), включить
Bot-to-Bot Mode и привязать одноразовым кодом.

Формальная часть ниже — источник истины для реализации. Расхождение кода и этой
спеки считается багом.

## 1. Идентичность и допуск

| Понятие | Значение |
|---|---|
| `owner` | tg_id владельца; все `bots/routes/tasks/links` привязаны к нему |
| `alias` | короткое имя бота в namespace владельца (`god`, `aa`), уникально в паре (owner, alias) |
| `bot_id` | Telegram user id бота; один бот принадлежит ровно одному владельцу |
| pairing | `/pair <code> <alias>` от бота в ЛС хабa; код одноразовый, выдаётся владельцем |
| доступ | по умолчанию **закрыт**: только tg_id из `TG_GATEWAY_OWNERS` (`TG_GATEWAY_OPEN=0`) |

## 2. Конверт

Все сообщения — один JSON-объект в одном сообщении Telegram.

```json
{"jsonrpc":"2.0","id":"<rpc-id>","method":"<method>","params":{...}}
```

Ответ: `result` либо `error{code,message}`. Коды: `-32700` parse, `-32600` invalid
request, `-32601` method not found, `-32602` invalid params, `-32603` internal,
`-32001` task not found, `-32002` task not cancelable, `-32003` бот не разрешён клиенту,
`-32004` unauthorized, `-32005` лимит частоты клиента.

### Общие правила доставки

| Правило | Значение |
|---|---|
| TTL конверта | `params.ttl_seconds` (по умолчанию **300**), метка `params.metadata.sent_at` (unix). Просроченное → `-32602` |
| Идемпотентность | `params.message.messageId` — повтор возвращает ту же задачу с `deduplicated: true` |
| Отпечаток | `sha256("<src>:<dst>:<text>")`, окно **300 с** — ловит повтор того же текста с новым `messageId` |
| Лимиты | `max_text` (3000) и `per_hour` (20) на маршрут |
| Фильтры | секреты (ключи, токены, «пароль: …»), shell-инъекции — отказ `-32602` |
| Глубина | `hops` ≤ **3**; 4-е сообщение не доставляется, задача → `failed(hop_limit)` |
| Здоровье пира | 3 неудачные доставки → пир блокируется на 300 с (`peer_blocked`) |

## 3. Методы

### 3.1 `message/send` — агент → хаб

```json
{"jsonrpc":"2.0","id":"r-1","method":"message/send","params":{
  "message":{"role":"user","messageId":"m-…","contextId":"ab12",
             "parts":[{"kind":"text","text":"…"}]},
  "to":"aa",
  "ttl_seconds":300,
  "metadata":{"hops":1,"sent_at":1795037000.0}}}
```

`to` — локальный алиас или `@username` бота, связанного через `/share` + `/link`
(межвладельческая связь). Итог: `result.task` — Task в состоянии `auth-required`
(ждём карточку владельца) либо сразу `working`, если подтверждение не требуется.

Нужно ли подтверждение — решается по цепочке (первое совпадение):

1. `clients.policy` внешнего клиента (`ask` | `auto`), если он не `inherit`;
2. `bots.a2a_policy` получателя;
3. `TG_GATEWAY_A2A_POLICY` (по умолчанию `ask`).

Для бот-задач вместо клиента берётся политика маршрута `(src → dst)`; новое исходящее
всегда спрашивает владельца, продолжение по тому же `ctx` — по политике маршрута.

`params.wait_seconds` (или `TG_GATEWAY_A2A_WAIT`, дефолт 0) — сколько хаб держит
соединение, дожидаясь клика/ответа агента, чтобы клиент без поллинга получил результат
в том же вызове. Максимум 100 с.

### 3.6 Результат задачи в `task`

- `artifacts[0].parts[0].text` — **ответ агента** (`artifactId = <task>-out`);
- `history` — сообщения задачи: `user` (вход) и `agent` (ответ); `historyLength`
  ограничивает их число (0 — без истории, не задано — все);
- входной текст без ответа виден в `history[0]` (роль `user`) и в `message/send` либо `working` (политика `auto` для продолжения).

### 3.2 `message/send` — хаб → агент (доставка задачи)

```json
{"jsonrpc":"2.0","id":"d-ab12","method":"message/send","params":{
  "message":{"role":"user","messageId":"m-<task>","contextId":"ab12",
             "parts":[{"kind":"text","text":"…"},
                      {"kind":"data","data":{"free_gb":31}}]},
  "metadata":{"task":"3fe6f7","hops":2,"from":"god","sent_at":1795037000.0}}}
```

Агент обязан обработать `parts` любого из трёх видов (см. §5) и ответить `tasks/update`.

### 3.3 `tasks/update` — агент → хаб (результат)

```json
{"jsonrpc":"2.0","id":"u-1","method":"tasks/update","params":{
  "task":"3fe6f7","contextId":"ab12",
  "status":{"state":"completed"},
  "artifact":{"artifactId":"out-1","parts":[{"kind":"text","text":"31 ГБ"}]}}}
```

`status.state`:
- `completed` + непустой artifact → ответ уходит инициатору;
- `completed` без artifact → `skipped` (гасит цепочку);
- `input-required` + `status.message.parts` → владельцу карточка-вопрос;
- `failed` → задача закрыта как failed.

### 3.4 `tasks/get`, `tasks/list`, `tasks/cancel`

```json
{"jsonrpc":"2.0","id":"g-1","method":"tasks/get","params":{"id":"3fe6f7"}}
{"jsonrpc":"2.0","id":"p-1","method":"tasks/list","params":{"since":1795037000.0,"limit":50}}
{"jsonrpc":"2.0","id":"c-1","method":"tasks/cancel","params":{"id":"3fe6f7"}}
```

`tasks/list` — **поллинг вместо ожидания пушей**: отдаёт задачи, где вызывающий бот
`src` или `dst`, с `updated_at > since`, и `cursor` для следующего запроса.
`tasks/cancel` — только инициатор; получателю уходит `tasks/cancel` от хаба.

### 3.5 `agent/card` — discovery

```json
{"jsonrpc":"2.0","id":"k-1","method":"agent/card","params":{"alias":"aa"}}
→ {"result":{"card":{"name":"@aa","capabilities":{...},"skills":[...]}}}
```

Без `alias` — карточка хаба и всех ботов владельца. `skills[].description` берётся из
`/describe <alias> …`.

## 4. Состояния задачи

```
auth-required ──клик владельца──▶ working ──▶ completed
      │                              │  ▲
      │                              │  └── input-required (вопрос владельцу)
      └── отказ ──▶ rejected         └──▶ failed | canceled
```

| Наше состояние | В A2A |
|---|---|
| `submitted` (ждёт карточку) | `auth-required` |
| `working` | `working` |
| `input-required` | `input-required` |
| `completed` / `failed` / `rejected` / `cancelled` | одноимённые (`canceled`) |
| `skipped` (нечего отвечать) | `completed` + `metadata.skipped: true` |

## 5. Parts

| kind | Поле | Что делает хаб |
|---|---|---|
| `text` | `text` | склеивается в тело сообщения |
| `data` | `data` (JSON) | сохраняется в `tasks.attachments`, доезжает до пира как `data`-часть, в модель агента попадает JSON-текстом |
| `file` | `file{name,mimeType,uri}` | бинарь **не** передаётся: доезжает как `file`-часть со ссылкой и строкой `[файл: name]` в теле |

Файловые блобы сознательно не поддерживаются: Telegram-канал не даёт гарантий
целостности, а агент не должен получать доступ к ФС по ссылке из недоверенного текста.

## 6. Человек в разрыве (расширение)

- Новый исходящий запрос → карточка владельцу с кнопками; без клика ничего не уходит.
- Продолжение по тому же `contextId` — по политике маршрута (`ask`/`auto`).
- Межвладельческая связь: `/share` (приглашение) + `/link`, подтверждения **с двух сторон**
  (`policy_src` и `policy_dst`).
- Карточка = единственная авторизация; кодов `APPROVE` нет.
- `capabilities.humanApproval: true` в карточке хаба — это про данное расширение.

## 7. Отношение к A2A и A2A-TG

- **A2A (Linux Foundation)**: берём словарь и структуры (Task, TaskStatus, Part,
  AgentCard, JSON-RPC методы), но не HTTP-транспорт и не streaming/push — их в
  Telegram нет (`capabilities.streaming/pushNotifications: false`).
- **A2A-TG (telegram-ai-bridge)**: похожая идея конверта с TTL и дедупом; отличия —
  у нас есть state machine, `tasks/*`, карточки владельца и мультитенантность, зато
  мы допускаем DM-транспорт (у них только группы, `chat_id < 0`).
- Совместимость с внешними A2A-агентами **не заявляется** и не поддерживается.

## 8. HTTP-фронт для внешних A2A-клиентов

Транспорт Telegram остаётся основным, но хаб умеет поднимать локальный A2A-сервер
(`hub/a2a_http.py`), чтобы к нему подключались стандартные A2A-клиенты:

- `GET /.well-known/agent-card.json` — карточка хаба (боты = skills);
- JSON-RPC `message/send` → задача создаётся в состоянии `auth-required` (нужна
  карточка владельцу), `tasks/get` / `tasks/list` / `tasks/cancel` — как обычно;
- авторизация: `Authorization: Bearer <TG_GATEWAY_A2A_TOKEN>`, биндинг по умолчанию
  127.0.0.1 (наружу — только через nginx с TLS);
- источник задачи помечается `src_alias = "a2a-client"`: ответ бота не порождает новую
  задачу боту, а закрывает задачу клиента (artifact в `tasks/get`);
- ограничения: `streaming=false`, `pushNotifications=false`, один токен = один владелец.

## 9. Контекст ветки (contextId)

`contextId` — не общая память, а ветка: хаб помнит задачи с этим `ctx` и передаёт
получателю сжатую выжимку предыдущих ходов.

- `GetTask` понимает `historyLength` (0 — без истории, N — последние N сообщений, не задано — все);
- `ListTasks` фильтрует по `contextId` (и `status`);
- при доставке боту в `message/parts` добавляется `{kind: "data", data: {context: [{from,to,
  question,answer,state}, ...]}}` — до 5 ходов, 240 символов на поле; в `metadata.contextTurns`
  число переданных ходов;
- выключается на маршруте (`context_mode='off'`, в меню — «🧠 Контекст → выкл»); для задач
  внешних клиентов берётся маршрут `'' → <bot>`;
- выжимка собирается только по своей ветке и только для получателя: агенты по-прежнему не
  видят чужие задачи и не имеют общего хранилища.

## 10. Клиенты и токены

Авторизация HTTP-фронта — Bearer. Токенов два вида:

- `TG_GATEWAY_A2A_TOKEN` — legacy, права владельца: все боты, все задачи, без лимитов;
- `tgw_<client_id>_<secret>` — внешний клиент: список ботов, политика (`inherit|ask|auto`),
  лимиты (`max_text`, `per_min`, `per_hour`), срок действия, отзыв.

Клиент создаётся в меню хаба («🔑 Клиенты»): токен показывается один раз, в БД только
sha256 и префикс. Настраивается: разрешённые боты, политика, `max_text`, `per_min`,
`per_hour`, срок действия (`expires_at`), ротация и отзыв (действуют сразу).

Проверки по клиенту: `-32003` (бот не разрешён), `-32005` (лимит частоты), задачи
фильтруются по `tasks.client_id`. Ошибка авторизации — HTTP 401 на любом эндпоинте.
Сессии задач одного клиента не видны другому. Истёкший или отозванный токен → 401,
событие `client_denied` с причиной (`bot`/`revoked`/`expired`).

## 11. Аудит

Хаб: таблица `events` (`relay_parked`, `card_approved`, `delivered`, `reply_parked`,
`a2a_parked`, `a2a_auto_delivered`, `a2a_answered`, `a2a_input_required`, `a2a_skipped`,
`client_created`, `client_auth`, `client_denied`, `client_rate_limited`,
`client_token_rotated`, `client_revoked`, `client_expiry_changed`, `client_bots_changed`,
`route_context_changed`, `menu_*`,
`task_skipped`, `ttl_rejected`, `rpc_dedup`, `rpc_dedup_fingerprint`, `hop_limit`,
`peer_blocked`, `peer_circuit_open`, `card_unauthorized`, `hub_error`).
Агент: `logs/gateway-relay-audit.jsonl` (`hub_inbound`, `rpc_tasks_update`,
`hub_rpc_result`, `inbound_answered`, `inbound_skipped`, `task_input_required`).
В аудит пишутся hash/длины, не текст.
