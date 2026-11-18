# AGENTS.md — правила для агентов в этом репозитории

Проект: **tg-gateway** — шлюз для подтверждаемого человеком межагентного обмена через
Telegram. Две части: `hub/` (отдельный бот-шлюз) и плагин агента (`core.py`, `state.py`).
Репозиторий — единственный источник кода; конфиги конкретных агентов лежат в их
инфраструктурных репозиториях (`~/workspace/infra/dotfiles/<agent>/hermes/...`).

## Структура

| Файл | Назначение |
|---|---|
| `hub/app.py` | Бот-хаб: pairing, маршруты, карточки владельцу, ретрансляция, ответы, отмена, TTL. |
| `hub/db.py` | SQLite: `owners`, `bots`, `pair_codes`, `routes`, `tasks`, `events`. |
| `hub/policy.py` | Чистые правила: когда спрашивать, лимиты, hop ≤ 3, фильтры секретов. |
| `hub/tg.py` | Клиент Bot API на stdlib (long polling, send/edit, callback). |
| `state.py` | Машина состояний задачи агента (карточка 20 мин, задача 24 ч, миграция). |
| `core.py` | Логика плагина: режимы `group` (карточки в группе/ЛС) и `endpoint` (провод к хабу). |
| `__init__.py` | `register(ctx)`: хук + фабрика callback-хендлера + регистрация инструментов. |
| `plugin.yaml` | Манифест: имя, версия, `requires_hermes`, `provides_tools`, `provides_hooks`. |
| `deploy/` | systemd user-unit хаба + `install.sh`. |
| `tests/test_hub.py` | Хаб: pairing, карточки, авто-продолжения, ask-цикл, лимиты, TTL. |
| `tests/test_gateway_relay_hook.py` | Плагин: хук, карточки, endpoint-режим. |
| `tests/test_state.py` | Переходы состояний, TTL, миграция, персистентность. |

## Проверка перед коммитом

```bash
python3 tests/test_state.py
python3 tests/test_gateway_relay_hook.py
python3 tests/test_hub.py
python3 -c "import ast; ast.parse(open('core.py').read())"
```

Тесты — контракт. Меняешь поведение — сначала тест, потом код.

## Инварианты

1. **Кнопка = подтверждение.** Отправка пиру — только после клика владельца
   (`owner_ids` в `group`, `owners` в хабе). Кодов `APPROVE` нет.
2. **Новый исходящий запрос спрашивает владельца; продолжение по тому же `ctx` — по
   политике маршрута** (`ask`/`auto`). Иначе карточки множатся на каждый ответ.
3. **Входящее не доходит до модели до подтверждения** (в `group` — карточка; в
   `endpoint` — подтверждение уже дано хабом).
4. **Анти-петля:** `[gw ctx:<id> #N]`, `N > 3` не принимается, `SKIP` гасит цепочку,
   `#cancel]` — уведомление, а не задача.
5. **Никакого shell/файлов/сети для модели.** `subprocess` — списком аргументов, без
   `shell=True`; HTTP — только через `_tg_api`/`Tg.call`.
6. **Адресаты структурны:** ключи реестра (`group`) или маршруты хаба (`endpoint`);
   `chat_id`/`thread_id`/URL из модели не принимаются.
7. **Аудит обязателен** для каждого решения; в аудит — hash и длина, не текст.
8. **Секреты не пересылаются:** `policy.scan_text` на хабе и `scan_text` в плагине.

## Telegram-грабли (проверено, не переоткрывать)

- **General (топик 1) не имеет `message_thread_id`** — API отвечает `message thread not
  found`. `approvals_thread: 1` = «слать без thread_id».
- **Telegram срезает наш `@handle`** у адресата — упоминание проверять по
  `event.raw_message.text`.
- **`require_mention` резолвится из профиля запуска**, не из маршрутизированного → гейт
  в хуке.
- **`PluginContext.inject_message`** не будит чат чужого профиля → тихий ход профиля
  (`hermes -p <profile> chat -q`).
- **`send_clarify`** всегда пишет в топик сессии → карточки рисует плагин/хаб.
- **DM бот↔бот работает** при включённом Bot-to-Bot Communication Mode у обеих сторон
  (проверено живьём), групповой чат для этого не нужен.

## Формат реестра агента (`gateway-routes.json`, runtime, не в git)

Режим `group`: `chat_id`, `approvals_thread`, `approvals_chat` (ЛС владельца — приоритет),
`owner_ids`, `limits`, `self`, `bots{...}`.
Режим `endpoint`: `mode: "endpoint"`, `hub{bot_id, username}`.
`GATEWAY_RELAY_HOME` переопределяет дом (тесты).

## Стиль

- Только stdlib. Новых зависимостей не добавлять.
- Русские строки в UI/аудите; идентификаторы кода — английские.
- Исключения в хуке не пробрасывать: `_audit("hook_error", ...)`.
- Версия в `plugin.yaml` — semver; ломающие изменения = minor+.

## Деплой

Хаб: `deploy/install.sh` (user-unit `tg-gateway-hub`, `EnvironmentFile=~/tg-gateway/.env`).

Плагин в режиме `group` копируется в **два** места у агента:

```
<profile-home>/plugins/gateway-relay/    # инструменты, watcher
<launch-home>/plugins/gateway-relay/     # хук + callback-фабрика
```

После копирования — перезапуск гейтвея (Docker — `docker restart <container>`, хостовый —
graceful `SIGUSR1` из `gateway_state.json`). Проверка: в логе
`Wired native handlers from plugin` и карточка в ЛС владельца.
