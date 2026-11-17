# AGENTS.md — правила для агентов в этом репозитории

Проект: **gateway-relay** — плагин Hermes для подтверждаемого человеком
межагентного обмена через Telegram-супергруппу. Репозиторий — единственный
источник кода плагина; конфиги конкретных агентов лежат в их инфраструктурных
репозиториях (у нас — `~/workspace/infra/dotfiles/<agent>/hermes/...`).

## Структура

| Файл | Назначение |
|---|---|
| `core.py` | Вся логика: реестр маршрутов, карточки, callback-резолвер, хук `pre_gateway_dispatch`, watcher, `ask_default`, `gateway_inbox`, аудит. |
| `__init__.py` | `register(ctx)`: хук + фабрика Telegram-callback-хендлера + регистрация инструментов по домам. |
| `plugin.yaml` | Манифест: имя, версия, `requires_hermes`, `provides_tools`, `provides_hooks`. |
| `tests/test_gateway_relay_hook.py` | Юнит-тесты без сети (подменяют `_routes/_state/_tg_*/_send_card`). |
| `README.md` | Для пользователя: установка, конфиг, модель безопасности. |

## Проверка перед коммитом

```bash
python3 tests/test_gateway_relay_hook.py     # должно быть OK, 0 падений
python3 -c "import ast; ast.parse(open('core.py').read())"
```

Тесты — контракт. Меняешь поведение хука/карточек — сначала тест, потом код.

## Инварианты, которые нельзя ломать

1. **Кнопка = подтверждение.** Отправка в чат происходит только из
   `resolve_card_click` после клика владельца (`owner_ids`). Никаких кодов
   `APPROVE`, никаких «подтверди текстом», никакой отправки из `relay_send`.
2. **Входящее не доходит до модели до клика.** Хук паркует сообщение бота и рисует
   карточку; текст попадает в модель только через тихий ход после «Принять».
3. **Два дома.** PTB-хендлеры берутся из менеджера плагинов **профиля запуска**.
   Если плагин установлен только в профиль `gateway`, кнопки не работают: нужна
   ingress-копия в launch-home и `plugins.enabled` там же.
4. **Анти-петля.** `[gw-hop:N]` в каждом исходящем; входящее с `N > 3` не
   паркуется. Тихий ход обязан отвечать `SKIP` на не-вопросы; мета-ответы не
   публикуются.
5. **Никакого shell/файлов/сети для модели.** Плагин даёт только relay-инструменты
   и `ask_default`/`gateway_inbox`. `subprocess` — только списком аргументов, без
   `shell=True`; HTTP — только через `_tg_api` (curl + Bot API).
6. **Адресаты структурны.** Только ключи из `gateway-routes.json`;
   `chat_id`/`thread_id`/URL из модели не принимаются.
7. **Аудит обязателен** для каждого решения: `card_sent`, `card_rejected`,
   `card_unauthorized`, `bot_inbound_parked`, `inbound_approved`,
   `inbound_answered`, `inbound_skipped`, `hop_limit`, `refused`,
   `rate_limited`. В аудит — hash и длина, не текст.

## Telegram-грабли (проверено, не переоткрывать)

- **General (топик 1) не имеет `message_thread_id`** — API отвечает
  `message thread not found`. `approvals_thread: 1` означает «слать без thread_id».
- **Telegram срезает наш `@handle`** из текста сообщения у адресата — упоминание
  проверяется по `event.raw_message.text`, а не по `event.text`.
- **`require_mention` резолвится из профиля запуска**, не из маршрутизированного —
  поэтому mention-гейт живёт в хуке.
- **`PluginContext.inject_message`** не будит чат чужого профиля (и упирается в
  авторизацию источника) — принятое сообщение обрабатывается тихим ходом профиля
  (`hermes -p <profile> chat -q`), а не инъекцией.
- **`send_clarify`** всегда публикует карточку в топик сессии — поэтому карточки
  рисует плагин, а не `clarify`.

## Формат реестра

`gateway-routes.json` (runtime, в домашнем каталоге профиля `gateway`, в git не
попадает): `chat_id`, `approvals_thread`, `owner_ids`, `limits`, `self`
(`bot_username`, `bot_id`), `bots` (`title`, `bot_username`, `bot_id`,
`chat_id`, `thread_id`, `enabled`). `GATEWAY_RELAY_HOME` переопределяет дом
(используется в тестах).

## Стиль

- Только stdlib. Новых зависимостей не добавлять.
- Русские строки в UI/аудите — как в существующем коде; идентификаторы кода —
  английские.
- Исключения в хуке никогда не пробрасывать: `_audit("hook_error", ...)` и
  обычная диспетчеризация.
- Версия в `plugin.yaml` — semver; ломающие изменения поведения = minor+.

## Деплой в живых агентов

Плагин копируется в **два** места у каждого агента (пример для sysadmin):

```
<profile-home>/plugins/gateway-relay/            # инструменты, watcher
<launch-home>/plugins/gateway-relay/             # хук + callback-фабрика
```

После копирования — перезапуск гейтвея агента (у Docker-агентов —
`docker restart <container>`; у хостового — graceful `SIGUSR1`). Проверка:
в логе `Wired native handlers from plugin 'gateway-relay'` и появление карточки
в топике подтверждений.
