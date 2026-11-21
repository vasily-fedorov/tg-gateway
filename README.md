# tg-gateway — хаб межагентного обмена

Telegram-бот, который держит маршруты, подтверждения владельца и состояние задач между
агентами. Плагин агента (Hermes) живёт отдельно:
[`hermes-tg-gateway`](https://github.com/vasily-fedorov/hermes-tg-gateway).

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
- лимиты (`max_text`, `per_hour`), глубина цепочки (hops ≤ 6), TTL конверта 300 с, дедуп по `messageId` и по отпечатку содержимого;
- здоровье пиров: 3 неудачные доставки → блокировка на 5 минут;
- мультитенантность: у каждого владельца свои боты, маршруты, задачи и аудит (межвладельческих связей нет — сняты);
- части сообщения: `text`, `data` (JSON), `file` (только ссылка, бинарь не передаётся);
- **контекст ветки**: задачи с одним `contextId` образуют ветку, получателю передаётся
  выжимка предыдущих ходов (5 ходов × 240 символов, отключается на маршруте).

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
/links, /unlink <id>         список связей; в меню — политики out/in и разрыв
/clients                     внешние A2A-клиенты: токены, права, лимиты, срок
/client <cl_xxxxxx>          карточка клиента
/token <cl_xxxxxx>           ротация токена клиента
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
TG_GATEWAY_A2A_PORT=18765          # 0 = выключен
TG_GATEWAY_A2A_HOST=127.0.0.1      # наружу — через nginx + TLS
TG_GATEWAY_A2A_TOKEN=<bearer>      # legacy-токен владельца
TG_GATEWAY_A2A_PUBLIC_URL=https://a2a.example.com
TG_GATEWAY_A2A_WAIT=75             # сколько ждать клика/ответа в message/send
TG_GATEWAY_A2A_PUBLIC_CARD=0       # 1 — карточка читается без токена (список агентов пуст)
EOF
chmod 600 ~/tg-gateway/.env
bash deploy/install.sh     # user-unit tg-gateway-hub
```

Боту в @BotFather нужны: **Bot-to-Bot Communication Mode** и членство в одной группе с
ботами-агентами (Telegram открывает bot-to-bot DM только при общем чате).

## Асинхронная модель

Хаб — **A2A-сервер**, а не релеграф с ожиданием:

- `message/send` возвращается сразу: `TASK_STATE_AUTH_REQUIRED` (политика `ask` — ждёт
  клика владельца) или `TASK_STATE_WORKING` (политика `auto`).
- `tasks/get` / `tasks/list` — состояние, прогресс, ответ (artifact), история.
- `tasks/update` — **внутренний** канал исполнителя (закрыт токеном бота):
  `working` = прогресс/хартбит, `input-required` = вопрос, `completed` = финал.
- `tasks/cancel` — только нетерминальные, пробрасывается исполнителю.
- Ответ на ответ новую задачу не создаёт (петля гасится на хабе) + дедуп по `messageId`.

## Исполнитель и клиент

- **Исполнитель** (профиль `gateway` агента): принял → `working` → делегирует работу
  основному профилю (`delegate_task(profile=default)`) → ответ присылает ребёнок
  через `relay_answer`/`a2a_answer`; текст модели gateway в хаб не попадает.
- **Клиент** (обычный профиль): `relay_send` (отправка), `relay_status` (= `tasks/get`),
  `relay_inbox` (ответы, пришедшие асинхронно), `relay_cancel`.

## Тесты

```bash
python3 tests/test_hub.py
```

## Аудит

SQLite-таблица `events`: `relay_parked`, `card_approved`, `delivered`, `reply_parked`,
`task_skipped`, `ttl_rejected`, `rpc_dedup`, `hop_limit`, `peer_blocked`,
`card_unauthorized`, `owner_log`, `hub_error`. Пишутся hash/длины, не текст.
