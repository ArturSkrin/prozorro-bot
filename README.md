# Prozorro → Telegram

Бот непрерывно читает ленту изменений публичного API Prozorro (`feed=changes`), по сохранённым поискам
находит тендеры и сразу шлёт их в Telegram с контактами победителя и замовника. Управление — из Telegram
и из веб-морды (Flask).

```
feed_watcher → очередь → detail_worker ×8 → match() → очередь Telegram
tg_poller (long polling getUpdates)          веб (отдельный процесс, та же SQLite в WAL)
```

## Установка и запуск

```bash
python3.12 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env    # заполнить TG_TOKEN, TG_CHAT_IDS, WEB_PASSWORD, WEB_SECRET
python -m bot           # бот
python -m web.app       # веб-морда на http://127.0.0.1:8080
python -m bot backfill 7   # разово: изменения за 7 дней (те же поиски и дедуп)
```

Docker (домашняя лаборатория): `cp .env.example .env`, заполнить, `docker compose up -d --build`.
БД лежит в `./data`. Веб слушает только `127.0.0.1:8080` хоста; наружу — через Cloudflare Tunnel
(`cloudflared tunnel --url http://127.0.0.1:8080` или именованный туннель), пароль из `WEB_PASSWORD` обязателен.

## Как добавить чат в whitelist

1. Добавьте бота в чат (или напишите ему в личку).
2. Узнайте chat_id: напишите в чат сообщение и откройте `https://api.telegram.org/bot<TOKEN>/getUpdates`
   (бота перед этим остановите — он сам читает обновления), поле `message.chat.id`. У групп id отрицательный.
3. Впишите в `.env`: `TG_CHAT_IDS=123456789,-1001234567890` и перезапустите бота.
   Остальным бот не отвечает. `TG_ALLOWED_USERS` — user_id, которым можно управлять ботом из других чатов.

## Telegram

- `/searches` — поиски с кнопками вкл/выкл/изменить/удалить; `/new` — мастер создания поиска;
  `/pause`, `/resume` — все уведомления; `/status` — курсор, отставание, ошибки API, отправки за сутки.
- Под тендером: «Беру» (первый нажавший фиксируется, у всех сообщение меняется на «Взяв Имя»),
  «Не цікаво», «Відкрити на Prozorro».
- Тендер шлётся один раз на пару (поиск, тендер). Если у поиска включено «надсилати зміни» (по умолчанию да),
  смена статуса или победителя приходит ответом на исходное сообщение.

## Как устроено

- Первый запуск встаёт на конец ленты (`descending=1`, `prev_page.offset`), история не выкачивается.
  Курсор (`next_page.offset`) сохраняется в БД после обработки каждой страницы, после рестарта продолжаем с него.
  Пустая страница — ждём `FEED_IDLE_SEC` и повторяем тот же offset.
- Предфильтр по полям ленты (status, procurementMethodType), детали (`GET /tenders/{id}`) — только для прошедших
  и для уже присланных (чтобы ловить изменения).
- Забезпечення пропозицій: `guarantee.amount > 0` (тендер или лоты) или критерий `CRITERION.OTHER.BID.GUARANTEE`.
  Забезпечення договору: критерий `CRITERION.OTHER.CONTRACT.GUARANTEE`.
- Победитель: `awards[]` со `status=active` → `suppliers[]`.

## Тесты

```bash
pip install pytest && python -m pytest -q
```
# prozorro-bot
