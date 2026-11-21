# tg-gateway protocol v1.1

Основной транспорт — **Telegram DM** (бот-хаб ↔ бот-агент). Контракт — **JSON-RPC 2.0**
с объектами A2A v1.0. Плагин агента живёт отдельно (`hermes-tg-gateway`).

Чтобы подключить бота с другого хоста через Telegram, достаточно добавить его в общую
супергруппу с хабом (Telegram открывает bot-to-bot DM только при общем чате), включить
Bot-to-Bot Mode и привязать одноразовым кодом.

Расхождение кода и этой спеки считается багом.

## 1. Идентичность и допуск

| Понятие | Значение |
|---|---|
| `owner` | tg_id владельца; все `bots/routes/tasks` привязаны к нему |
| `alias` | короткое имя бота в namespace владельца (`god`, `aa`), уникально в паре (owner, alias) |
| `bot_id` | Telegram user id бота; один бот принадлежит ровно одному владельцу |
| pairing | `/pair <alias>` → код в ЛС владельца → `/pair <код> <alias>` от бота |
| доступ | по умолчанию **закрыт**: только tg_id из `TG_GATEWAY_OWNERS` (`TG_GATEWAY_OPEN=0`) |

Межвладельческих связей и внешних A2A-клиентов нет: хаб работает с парами
«владелец + его боты».

## 2. Конверт

```json
{"jsonrpc":"2.0","id":"<rpc-id>","method":"<method>","params":{...}}
```

Ответ: `result` либо `error{code,message}`. Коды: `-32700` parse, `-32600` invalid
request, `-32601` method not found, `-32602` invalid params, `-32603` internal,
`-32001` task not found, `-32002` task not cancelable, `-32003` бот не разрешён.

### Общие правила доставки

| Правило | Значение |
|---|---|
| TTL конверта | `params.ttl_seconds` (по умолчанию **300**), метка `metadata.sent_at` (unix). Просроченное → `-32602` |
| Идемпотентность | `params.message.messageId` — повтор возвращает ту же задачу с `deduplicated: true` |
| Отпечаток | `sha256("<src>:<dst>:<text+attachments>")`, окно **300 с** |
| Лимиты | `max_text` (3000) и `per_hour` (20) на маршрут |
| Глубина | `hops ≤ 6`; возврат ответа глубину **не** увеличивает |
| Здоровье пира | 3 неудачные доставки → блокировка на 300 с (`peer_blocked`) |
| Петля | ответ на ответ (`direction=reply`) новую задачу не создаёт |

## 3. Методы

> **Асинхронность обязательна.** `message/send` возвращается сразу: задача создана,
> состояние `TASK_STATE_AUTH_REQUIRED` (ждёт подтверждения владельца) или
> `TASK_STATE_WORKING` (политика `auto`). Результат забирают `tasks/get` / `tasks/list`.
> Синхронного ожидания внутри вызова нет ни у одной стороны.

Публичная поверхность — только spec-методы (`message/send`, `tasks/get`, `tasks/list`,
`tasks/cancel`, `agent/card`). `tasks/update` — **внутренний** канал исполнителя,
закрытый токеном бота.

### 3.1 `message/send` — клиент (default-профиль) → хаб

```json
{"jsonrpc":"2.0","id":"r-1","method":"message/send","params":{
  "message":{"role":"user","messageId":"m-…","contextId":"ab12",
             "parts":[{"kind":"text","text":"…"}]},
  "to":"aa",
  "metadata":{"hops":1,"transport":"telegram"}}}
```

`to` — алиас бота владельца. Итог: `result.task` — Task в `auth-required` (нужна карточка)
или `working` (маршрут `auto`). `params.wait_seconds` больше не используется: ожидания нет.

Решение о подтверждении — по маршруту `(src → dst)`:

- `policy=ask` (по умолчанию) → карточка владельцу, задача в `auth-required`, пока не кликнут;
- `policy=auto` → доставка сразу, задача `working`.

### 3.2 `message/send` — хаб → агент (доставка задачи)

```json
{"jsonrpc":"2.0","id":"d-ab12","method":"message/send","params":{
  "message":{"role":"user","messageId":"m-<task>","contextId":"ab12",
             "parts":[{"kind":"text","text":"…"},
                      {"kind":"data","data":{"context":[…]}}]},
  "metadata":{"task":"3fe6f7","hops":2,"from":"god","sent_at":1795037000.0,
              "kind":"request"}}}
```

`metadata.kind`:

- `request` — задача, исполнитель обязан ответить `tasks/update`;
- `answer` — **ответ** на прежнюю задачу: исполнитель кладёт текст в инбокс и хода не
  делает; хаб закрывает такую задачу сам при доставке.

### 3.3 `tasks/update` — агент → хаб (внутренний канал)

```json
{"jsonrpc":"2.0","id":"u-1","method":"tasks/update","params":{
  "task":"3fe6f7",
  "status":{"state":"TASK_STATE_WORKING",
            "message":{"parts":[{"kind":"text","text":"принял, выполняю"}]}}}}
```

Разбор **state-aware** (принимаются и `TASK_STATE_*`, и короткие имена):

- `working` / `submitted` / `auth-required` → прогресс + хартбит: задача остаётся живой,
  текст попадает в `tasks.progress` и в историю (`event: task_progress`);
- `input-required` → вопрос по существу: `status.question`, карточка владельцу;
- `completed` + `artifact.parts[]` → финальный ответ (уходит инициатору);
- `completed` без artifact → `skipped` (гасит цепочку);
- `failed` / `canceled` / `rejected` → терминальное состояние с причиной в `progress`.

По уже закрытой задаче апдейты игнорируются (`late_update_ignored`,
`late_progress_ignored`).

### 3.4 `tasks/get`, `tasks/list`, `tasks/cancel`

```json
{"jsonrpc":"2.0","id":"g-1","method":"tasks/get","params":{"id":"3fe6f7","historyLength":10}}
{"jsonrpc":"2.0","id":"p-1","method":"tasks/list","params":{"limit":20,"cursor":0,"status":"working"}}
{"jsonrpc":"2.0","id":"c-1","method":"tasks/cancel","params":{"id":"3fe6f7"}}
```

`tasks/get` отдаёт:

```json
{"task":{
  "id":"3fe6f7","contextId":"ab12","kind":"task",
  "status":{"state":"TASK_STATE_WORKING","timestamp":"…",
            "message":{"role":"agent","parts":[{"kind":"text","text":"выполняю, 3 мин"}]}},
  "artifacts":[{"artifactId":"3fe6f7-out","parts":[{"kind":"text","text":"<ответ>"}]}],
  "history":[{"role":"user","parts":[…]},{"role":"agent","parts":[…]}],
  "metadata":{"progress":"…","heartbeat":"…","updatedAt":"…","from":"god","to":"aa",
              "humanApproval":true}}}
```

- `artifacts` появляются только у завершённой задачи; `history` — переписка задачи
  (`task_messages`), `historyLength` ограничивает число сообщений;
- `tasks/list` сортирует по `updated_at DESC`, отдаёт `nextCursor` (курсор вниз);
- `tasks/cancel` — только нетерминальные; исполнителю уходит `tasks/cancel`.

### 3.5 `agent/card` — discovery

```json
{"jsonrpc":"2.0","id":"k-1","method":"agent/card","params":{"alias":"aa"}}
→ {"result":{"card":{"name":"@aa","capabilities":{…},"skills":[…]},
             "routes":[…]}}
```

Без `alias` — своя карточка + `agents[]` + `routes[]`. `routes[]` — актуальный список
адресатов для спрашивающего бота с `policy`/`max_text`/`per_hour`/`context`.
Это источник истины; локальный `gateway-routes.json` у агента — только адрес хаба и фолбэк.

## 4. Состояния задачи

```
auth-required ──клик владельца──▶ working ──▶ completed
      │                              │  ▲
      │                              │  └── input-required (вопрос)
      └── отказ ──▶ rejected         └──▶ failed | canceled
```

| Наше состояние | A2A `TASK_STATE_*` |
|---|---|
| `submitted` (ждёт карточку) | `AUTH_REQUIRED` |
| `working` | `WORKING` |
| `input-required` | `INPUT_REQUIRED` |
| `completed` / `failed` / `rejected` / `cancelled` | одноимённые (`CANCELED`) |
| `skipped` (нечего отвечать) | `COMPLETED` + `metadata.skipped: true` |

`AUTH_REQUIRED` — штатное interrupted-состояние спеки: блокирующий клиент обязан получить
ответ на нём, поэтому клик владельца протокол не ломает.

## 5. Parts

| kind | Поле | Что делает хаб |
|---|---|---|
| `text` | `text` | склеивается в тело сообщения |
| `data` | `data` (JSON) | сохраняется в `tasks.attachments`, доезжает до пира как `data`-часть |
| `file` | `file{name,mimeType,uri}` | бинарь **не** передаётся: доезжает ссылкой + `[файл: name]` |

## 6. Роли: клиент, исполнитель, хаб

- **Клиент** — обычный профиль агента. Инструменты: `relay_send` (отправка),
  `relay_status` (= `tasks/get`), `relay_inbox` (ответы, пришедшие асинхронно),
  `relay_cancel`. Нативный `a2a_call` использовать не нужно: он синхронный.
- **Исполнитель** — профиль `gateway` агента, единственный вход: применяет политику
  (скан/рамка/redaction), сразу отвечает `working`, работу отдаёт
  `delegate_task(profile="default")`, ответ присылает ребёнок инструментом
  `relay_answer(task_id, text)`. Текст модели gateway в хаб не попадает.
- **Хаб** — A2A-сервер и владелец состояния: карточки, маршруты, прогресс, история,
  артефакты. Плагин задач не хранит.

Алиасы `a2a_answer`/`a2a_inbox` существуют, но платформа не отдаёт модели имена с
префиксом `a2a_` — рабочие имена `relay_*`.

## 7. Человек в разрыве

- Новый исходящий запрос по маршруту `ask` → карточка владельцу с кнопками; без клика
  ничего не уходит; снаружи это `auth-required`.
- Продолжение по тому же `contextId` — по политике маршрута.
- `input-required` — это вопрос по существу (не путать с разрешением на отправку).
- Карточка — единственная авторизация; кодов `APPROVE` нет.
- `capabilities.humanApproval: true` в карточке хаба — про это расширение.

## 8. Отношение к A2A и A2A-TG

- **A2A (Linux Foundation)**: совпадают структуры (Task, TaskStatus, Part, AgentCard),
  методы (`message/send`, `tasks/get|list|cancel`) и асинхронная модель
  (`returnImmediately` + опрос). Транспорт — Telegram, поэтому `streaming` и
  `pushNotifications` честно `false`.
- **A2A-TG (telegram-ai-bridge)**: похожий конверт с TTL и дедупом; у нас есть машина
  состояний, `tasks/*`, карточки владельца и прогресс задачи.
- Совместимость с внешними A2A-агентами по HTTP не поддерживается (фронт снят).

## 9. Контекст ветки (contextId)

`contextId` — ветка, не общая память: хаб помнит задачи с этим `ctx` и передаёт получателю
сжатую выжимку прошлых ходов.

- `tasks/get` понимает `historyLength` (0 — без истории, N — последние N, не задано — все);
- `tasks/list` фильтрует по `contextId` и `status`;
- при доставке в `message/parts` добавляется
  `{kind:"data", data:{context:[{from,to,question,answer,state}, …]}}` — до 5 ходов,
  240 символов на поле; число ходов в `metadata.contextTurns`;
- выключается на маршруте (`context_mode='off'`, меню «🧠 Контекст → выкл»).

## 10. Видимость маршрутов (routes_mode)

- `explicit` (**по умолчанию**) — бот видит только себя и тех, на кого есть явный маршрут;
  только что привязанный бот не видит никого и не виден другим;
- `all` — видит всех ботов владельца; адресат без маршрута доступен с `ask`.

Скрытым бот не попадает в `agent/card.agents`/`routes`, отправка отклоняется «нет маршрута»
(`hidden_dst_refused`), существование адресата не подтверждается. Переключатель — меню
бота → «👁 Маршруты».

## 11. Аудит

Хаб (`events`): `relay_parked`, `relay_auto`, `card_approved`, `card_rejected`, `delivered`,
`deliver_failed`, `task_progress`, `task_completed`, `task_input_required`, `task_skipped`,
`task_failed`, `task_cancelled`, `reply_parked`, `reply_auto`, `late_update_ignored`,
`late_progress_ignored`, `card_expired`, `ttl_rejected`, `rpc_dedup`,
`rpc_dedup_fingerprint`, `hop_limit`, `peer_blocked`, `peer_circuit_open`,
`card_unauthorized`, `hidden_dst_refused`, `route_context_changed`, `bot_described`,
`owner_log`, `menu_*`.

Агент (`logs/gateway-relay-audit.jsonl`): `hub_inbound`, `hub_answer_inbox`,
`endpoint_delegate`, `endpoint_turn_done`, `endpoint_answer`, `endpoint_skip_inflight`,
`rpc_tasks_update`, `rpc_tasks_get`, `hub_rpc_result`, `inbound_refused`,
`inbound_danger_neutralized`, `inbound_code_annotated`, `ask_default_failed`,
`profile_turn_error`, `inbox_hint`.

В аудит пишутся длины, правила и хэши — не текст сообщений.
