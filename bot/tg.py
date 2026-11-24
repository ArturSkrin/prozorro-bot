"""Telegram Bot API через httpx: long polling, очередь отправки, кнопки, команды, мастер поиска."""
import asyncio
import html
import json
import logging
import time

import httpx

from . import config, db
from .match import DEFAULT_PARAMS, STATUSES, bid_guarantee, has_contract_guarantee, winners

log = logging.getLogger("tg")
esc = html.escape


def money(x, cur=""):
    return f"{x:,.2f}".replace(",", " ") + (f" {cur}" if cur else "") if x is not None else "—"


def contacts(org):
    cp = org.get("contactPoint") or {}
    lines = [f"<b>{esc(org.get('name') or '—')}</b>, ЄДРПОУ {esc((org.get('identifier') or {}).get('id') or '—')}"]
    if cp.get("name"):
        lines.append(f"👤 {esc(cp['name'])}")
    if cp.get("telephone"):
        lines.append(f"📞 {esc(cp['telephone'])}")  # простым текстом — Telegram сам сделает кликабельным
    if cp.get("email"):
        lines.append(f"✉️ {esc(cp['email'])}")
    return "\n".join(lines)


def tender_text(t, search_names, delay):
    v = t.get("value") or {}
    bg = bid_guarantee(t)
    cur = v.get("currency", "")
    g = (t.get("guarantee") or {}).get("currency") or cur
    parts = [
        f"🔔 <b>{esc(t.get('tenderID', ''))}</b> · {esc(STATUSES.get(t.get('status'), t.get('status', '')))}",
        esc(t.get("title") or ""),
        f"💰 {money(v.get('amount'), cur)}",
        "🛡 Забезпечення пропозицій: " + ("немає" if bg is None else "є" if bg == 0 else money(bg, g))
        + " · договору: " + ("є" if has_contract_guarantee(t) else "немає"),
    ]
    for w in winners(t):
        parts.append("🏆 Переможець: " + contacts(w))
    parts.append("🏛 Замовник: " + contacts(t.get("procuringEntity") or {}))
    parts.append("🔎 Пошук: " + esc(", ".join(search_names)))
    parts.append(f"⏱ Затримка: {delay:.0f} с")
    parts.append(f"https://prozorro.gov.ua/tender/{t.get('tenderID')}")
    return "\n".join(parts)


def tender_kb(t, taken=False):
    row = [] if taken else [{"text": "✋ Беру", "callback_data": f"take:{t['id']}"},
                            {"text": "🚫 Не цікаво", "callback_data": f"skip:{t['id']}"}]
    return {"inline_keyboard": [row, [{"text": "Відкрити на Prozorro",
                                       "url": f"https://prozorro.gov.ua/tender/{t.get('tenderID')}"}]]}


# Шаги мастера: (ключ, тип, вопрос). "-" — очистить/пропустить, "." — оставить как было (при изменении).
STEPS = [
    ("name", "text", "Назва пошуку?"),
    ("statuses", "statuses", "Статуси (натисніть потрібні, потім «Далі»):"),
    ("amount_min", "num", "Очікувана вартість ВІД (число або «-»):"),
    ("amount_max", "num", "Очікувана вартість ДО (число або «-»):"),
    ("currency", "text", "Валюта (UAH, EUR… або «-»):"),
    ("guarantees", "flags", "Вимагати забезпечення (натисніть, потім «Далі»):"),
    ("method_types", "list", "Типи процедури через кому (aboveThreshold, belowThreshold… або «-»):"),
    ("cpv", "list", "Префікси CPV через кому (напр. 45, 0913 або «-»):"),
    ("regions", "list", "Регіони замовника через кому (напр. Київ, Львівська або «-»):"),
    ("keywords", "list", "Ключові слова в назві через кому (або «-»):"),
    ("minus_words", "list", "Мінус-слова через кому (або «-»):"),
    ("edrpou_allow", "list", "ЄДРПОУ замовників — білий список (або «-»):"),
    ("edrpou_deny", "list", "ЄДРПОУ замовників — чорний список (або «-»):"),
]
FLAGS = {"bid_guarantee": "пропозицій", "contract_guarantee": "договору", "send_changes": "надсилати зміни"}


class Tg:
    def __init__(self, con):
        self.con = con
        self.http = httpx.AsyncClient(base_url=f"https://api.telegram.org/bot{config.TG_TOKEN}/",
                                      timeout=httpx.Timeout(70, connect=10))
        self.queue = asyncio.Queue()
        self.wizards = {}  # chat_id -> {"step": i, "sid": id|None, "name":…, "params":…, "send_changes":…}

    async def call(self, method, **params):
        while True:
            try:
                r = await self.http.post(method, json=params)
                data = r.json()
            except (httpx.TransportError, ValueError) as e:
                log.warning("telegram %s: %s", method, type(e).__name__)
                await asyncio.sleep(3)
                continue
            if data.get("ok"):
                return data["result"]
            retry = (data.get("parameters") or {}).get("retry_after")
            if r.status_code == 429 and retry:
                await asyncio.sleep(retry + 0.5)
                continue
            log.warning("telegram %s: %s", method, data.get("description"))
            return None

    # --- отправка -------------------------------------------------------
    async def sender(self):
        """Очередь отправки. shortcut: глобальный темп 20 msg/s + retry_after; для больших групп поднять паузу до 3 с/чат."""
        while True:
            tender_id, chat_id, text, kb, reply_to = await self.queue.get()
            extra = {"reply_parameters": {"message_id": reply_to, "allow_sending_without_reply": True}} if reply_to else {}
            msg = await self.call("sendMessage", chat_id=chat_id, text=text, parse_mode="HTML",
                                  reply_markup=kb, link_preview_options={"is_disabled": True}, **extra)
            if msg and kb:
                self.con.execute("INSERT INTO messages VALUES(?,?,?,?)", (tender_id, chat_id, msg["message_id"], text))
            self.queue.task_done()
            await asyncio.sleep(0.05)

    def notify(self, t, search_names_by_chat, delay):
        for chat_id, names in search_names_by_chat.items():
            self.queue.put_nowait((t["id"], chat_id, tender_text(t, names, delay), tender_kb(t), None))

    def notify_change(self, t, chat_ids, text):
        for chat_id in chat_ids:
            row = self.con.execute("SELECT message_id FROM messages WHERE tender_id=? AND chat_id=? ORDER BY rowid DESC",
                                   (t["id"], chat_id)).fetchone()
            self.queue.put_nowait((t["id"], chat_id, text, None, row[0] if row else None))

    # --- приём ----------------------------------------------------------
    def allowed(self, chat_id, user_id):
        return chat_id in config.TG_CHAT_IDS or user_id in config.TG_ALLOWED_USERS

    async def poller(self):
        offset = int(db.get_state(self.con, "tg_offset", 0))
        while True:
            updates = await self.call("getUpdates", offset=offset, timeout=50,
                                      allowed_updates=["message", "callback_query"])
            for u in updates or []:
                offset = u["update_id"] + 1
                try:
                    await self.handle(u)
                except Exception as e:  # одно кривое обновление не должно ронять опрос
                    log.exception("update %s", u.get("update_id"))
                    db.log_error(self.con, f"tg update: {e}")
            db.set_state(self.con, "tg_offset", offset)
            if updates is None:
                await asyncio.sleep(3)

    async def handle(self, u):
        if "callback_query" in u:
            q = u["callback_query"]
            msg = q.get("message") or {}
            chat_id = (msg.get("chat") or {}).get("id")
            if not self.allowed(chat_id, q["from"]["id"]):
                return  # чужим не отвечаем
            return await self.on_button(q, chat_id, msg)
        m = u.get("message") or {}
        chat_id, user_id = m.get("chat", {}).get("id"), (m.get("from") or {}).get("id")
        if not self.allowed(chat_id, user_id):
            return
        text = (m.get("text") or "").strip()
        cmd = text.split()[0].split("@")[0] if text.startswith("/") else None
        if cmd:
            self.wizards.pop(chat_id, None)  # любая команда прерывает мастер
        if cmd == "/start" or cmd == "/help":
            await self.say(chat_id, "Команди: /searches /new /pause /resume /status")
        elif cmd == "/searches":
            await self.list_searches(chat_id)
        elif cmd == "/new":
            await self.start_wizard(chat_id, None)
        elif cmd in ("/pause", "/resume"):
            db.set_state(self.con, "paused", 1 if cmd == "/pause" else 0)
            await self.say(chat_id, "⏸ Сповіщення на паузі" if cmd == "/pause" else "▶️ Сповіщення увімкнено")
        elif cmd == "/status":
            await self.say(chat_id, status_text(self.con))
        elif chat_id in self.wizards and text:
            await self.wizard_text(chat_id, text)

    async def say(self, chat_id, text, kb=None):
        return await self.call("sendMessage", chat_id=chat_id, text=text, parse_mode="HTML", reply_markup=kb,
                               link_preview_options={"is_disabled": True})

    # --- кнопки под тендером --------------------------------------------
    async def on_button(self, q, chat_id, msg):
        action, _, arg = q.get("data", "").partition(":")
        name = q["from"].get("first_name") or q["from"].get("username") or str(q["from"]["id"])
        if action == "take":
            taken = self.con.execute("SELECT taken_by FROM matches WHERE tender_id=? AND state='taken'", (arg,)).fetchone()
            if taken:
                return await self.call("answerCallbackQuery", callback_query_id=q["id"], text=f"Вже взяв {taken[0]}")
            self.con.execute("UPDATE matches SET state='taken', taken_by=? WHERE tender_id=?", (name, arg))
            await self.call("answerCallbackQuery", callback_query_id=q["id"], text="Ваш!")
            t = json.loads(self.con.execute("SELECT json FROM tenders WHERE id=?", (arg,)).fetchone()[0])
            for c, mid, text in self.con.execute("SELECT chat_id, message_id, text FROM messages WHERE tender_id=?", (arg,)).fetchall():
                await self.call("editMessageText", chat_id=c, message_id=mid, parse_mode="HTML",
                                text=text + f"\n\n✅ <b>Взяв {esc(name)}</b>", reply_markup=tender_kb(t, taken=True),
                                link_preview_options={"is_disabled": True})
        elif action == "skip":
            self.con.execute("UPDATE matches SET state='skip' WHERE tender_id=? AND state='new'", (arg,))
            await self.call("answerCallbackQuery", callback_query_id=q["id"], text="Позначено: не цікаво")
            await self.call("editMessageReplyMarkup", chat_id=chat_id, message_id=msg["message_id"],
                            reply_markup={"inline_keyboard": [[{"text": f"🚫 Не цікаво ({name})", "callback_data": "noop"}]]})
        elif action == "sx":
            await self.call("answerCallbackQuery", callback_query_id=q["id"])
            await self.on_search_button(chat_id, arg)
        elif action == "wz":
            await self.call("answerCallbackQuery", callback_query_id=q["id"])
            await self.wizard_button(chat_id, arg)
        else:
            await self.call("answerCallbackQuery", callback_query_id=q["id"])

    # --- /searches ------------------------------------------------------
    async def list_searches(self, chat_id):
        items = db.searches(self.con)
        if not items:
            return await self.say(chat_id, "Пошуків немає. /new — створити.")
        for s in items:
            kb = {"inline_keyboard": [[
                {"text": "⏸ Вимкнути" if s["enabled"] else "▶️ Увімкнути", "callback_data": f"sx:toggle:{s['id']}"},
                {"text": "✏️ Змінити", "callback_data": f"sx:edit:{s['id']}"},
                {"text": "🗑 Видалити", "callback_data": f"sx:del:{s['id']}"}]]}
            await self.say(chat_id, search_text(s), kb)

    async def on_search_button(self, chat_id, arg):
        op, _, sid = arg.partition(":")
        s = db.get_search(self.con, int(sid))
        if not s:
            return await self.say(chat_id, "Пошук не знайдено.")
        if op == "toggle":
            db.save_search(self.con, s["name"], s["params"], s["id"], not s["enabled"], s["chat_ids"], s["send_changes"])
            await self.say(chat_id, f"«{esc(s['name'])}»: " + ("вимкнено" if s["enabled"] else "увімкнено"))
        elif op == "edit":
            await self.start_wizard(chat_id, s)
        elif op == "del":
            await self.say(chat_id, f"Видалити «{esc(s['name'])}»?", {"inline_keyboard": [[
                {"text": "Так, видалити", "callback_data": f"sx:delok:{s['id']}"}]]})
        elif op == "delok":
            db.delete_search(self.con, s["id"])
            await self.say(chat_id, f"«{esc(s['name'])}» видалено.")

    # --- мастер /new и «Змінити» ----------------------------------------
    async def start_wizard(self, chat_id, s):
        self.wizards[chat_id] = {"step": 0, "sid": s and s["id"], "name": s["name"] if s else "",
                                 "params": {**DEFAULT_PARAMS, **(s["params"] if s else {})},
                                 "send_changes": s["send_changes"] if s else 1, "chat_ids": s["chat_ids"] if s else []}
        await self.ask(chat_id)

    async def ask(self, chat_id):
        w = self.wizards[chat_id]
        key, kind, prompt = STEPS[w["step"]]
        cur = w["name"] if key == "name" else w["params"].get(key)
        hint = f"\nЗараз: <code>{esc(str(cur))}</code> («.» — залишити)" if w["sid"] and kind not in ("statuses", "flags") else ""
        if kind == "statuses":
            kb = [[{"text": ("✅ " if c in w["params"]["statuses"] else "") + n, "callback_data": f"wz:st:{c}"}]
                  for c, n in STATUSES.items()]
        elif kind == "flags":
            vals = {**w["params"], "send_changes": w["send_changes"]}
            kb = [[{"text": ("✅ " if vals.get(f) else "▫️ ") + n, "callback_data": f"wz:fl:{f}"}] for f, n in FLAGS.items()]
        else:
            return await self.say(chat_id, prompt + hint)
        kb.append([{"text": "Далі ➡️", "callback_data": "wz:next:"}])
        await self.say(chat_id, prompt, {"inline_keyboard": kb})

    async def wizard_button(self, chat_id, arg):
        w = self.wizards.get(chat_id)
        if not w:
            return
        op, _, val = arg.partition(":")
        if op == "st":
            st = w["params"]["statuses"]
            st.remove(val) if val in st else st.append(val)
            return await self.ask(chat_id)
        if op == "fl":
            if val == "send_changes":
                w["send_changes"] = int(not w["send_changes"])
            else:
                w["params"][val] = not w["params"].get(val)
            return await self.ask(chat_id)
        await self.next_step(chat_id)

    async def wizard_text(self, chat_id, text):
        w = self.wizards[chat_id]
        key, kind, _ = STEPS[w["step"]]
        if kind in ("statuses", "flags"):
            return await self.say(chat_id, "Натисніть кнопки вище і «Далі».")
        if text != ".":
            clear = text == "-"
            if kind == "num":
                try:
                    val = None if clear else float(text.replace(" ", "").replace(",", "."))
                except ValueError:
                    return await self.say(chat_id, "Потрібне число, напр. 3000000, або «-».")
            elif kind == "list":
                val = [] if clear else [x.strip() for x in text.split(",") if x.strip()]
            else:
                val = None if clear else text
            if key == "name":
                if not val:
                    return await self.say(chat_id, "Назва обов'язкова.")
                w["name"] = val
            else:
                w["params"][key] = val.upper() if key == "currency" and val else val
        await self.next_step(chat_id)

    async def next_step(self, chat_id):
        w = self.wizards[chat_id]
        w["step"] += 1
        if w["step"] < len(STEPS):
            return await self.ask(chat_id)
        self.wizards.pop(chat_id)
        sid = db.save_search(self.con, w["name"], w["params"], w["sid"], 1, w["chat_ids"], w["send_changes"])
        await self.say(chat_id, "💾 Збережено:\n" + search_text(db.get_search(self.con, sid)))


def search_text(s):
    p = {**DEFAULT_PARAMS, **s["params"]}
    bits = []
    if p["statuses"]:
        bits.append("статус: " + ", ".join(STATUSES.get(x, x) for x in p["statuses"]))
    if p["amount_min"] is not None or p["amount_max"] is not None:
        bits.append(f"сума: {money(p['amount_min'])} … {money(p['amount_max'])} {p['currency'] or ''}")
    if p["bid_guarantee"]:
        bits.append("є забезп. пропозицій")
    if p["contract_guarantee"]:
        bits.append("є забезп. договору")
    for k, label in [("method_types", "процедура"), ("cpv", "CPV"), ("regions", "регіон"), ("keywords", "слова"),
                     ("minus_words", "мінус"), ("edrpou_allow", "білий"), ("edrpou_deny", "чорний")]:
        if p[k]:
            bits.append(f"{label}: {', '.join(p[k])}")
    head = ("🟢" if s["enabled"] else "⚪️") + f" <b>{esc(s['name'])}</b>" + (" · зміни" if s["send_changes"] else "")
    return head + "\n" + esc("; ".join(bits) or "без фільтрів")


def status_text(con):
    day = time.time() - 86400
    lag = db.get_state(con, "lag_sec")
    errors = con.execute("SELECT ts, text FROM errors ORDER BY ts DESC LIMIT 5").fetchall()
    lines = [
        "⏸ НА ПАУЗІ" if db.get_state(con, "paused") == "1" else "▶️ Працює",
        f"Курсор: <code>{esc(db.get_state(con, 'feed_offset') or '—')}</code>",
        f"Останнє зміна в стрічці: {esc(db.get_state(con, 'last_modified') or '—')}",
        f"Відставання: {float(lag):.0f} с" if lag else "Відставання: —",
        f"Надіслано за добу: {con.execute('SELECT COUNT(*) FROM matches WHERE sent_at > ?', (day,)).fetchone()[0]}",
        "Помилки API:" if errors else "Помилок API немає",
    ] + [f"• {time.strftime('%H:%M:%S', time.localtime(ts))} {esc(t)}" for ts, t in errors]
    return "\n".join(lines)
