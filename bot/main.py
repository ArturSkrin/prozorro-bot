"""Процесс бота: feed_watcher → очередь → detail_worker → match → notifier; отдельно tg_poller.

python -m bot              — работать
python -m bot backfill 7   — разово пройти изменения за 7 дней (то же сопоставление и дедуп)
"""
import asyncio
import json
import logging
import signal
import sys
import time
from datetime import datetime, timedelta, timezone

import httpx

from . import config, db
from .match import STATUSES, match, prefilter, winners
from .prozorro import Api
from .tg import Tg

log = logging.getLogger("bot")


def ts(iso):
    return datetime.fromisoformat(iso).timestamp()


def winner_key(t):
    return ",".join(sorted((s.get("identifier") or {}).get("id", "") for s in winners(t)))


def wanted(item, searches, matched):
    """Нужны ли детали: проходит предфильтр хоть одного поиска или уже был прислан (следим за изменениями)."""
    return item["id"] in matched or any(prefilter(item, s["params"]) for s in searches)


async def process(con, tg, t):
    old = db.upsert_tender(con, t, winner_key(t))
    new = []
    for s in db.searches(con, enabled_only=True):
        ok, reasons = match(t, s)
        if ok and db.add_match(con, s["id"], t["id"], reasons):
            new.append(s)
    if db.get_state(con, "paused") == "1":
        return  # совпадения записаны (видны в вебе), но не шлём
    delay = time.time() - ts(t["dateModified"])
    if new:
        by_chat = {}
        for s in new:
            for c in s["chat_ids"] or config.TG_CHAT_IDS:
                by_chat.setdefault(c, []).append(s["name"])
        tg.notify(t, by_chat, delay)
        log.info("отправлено %s (%s), задержка %.1f с", t.get("tenderID"), ", ".join(s["name"] for s in new), delay)
        return
    if not old:
        return
    changes = []
    if old["status"] != t.get("status"):
        changes.append(f"статус: {STATUSES.get(old['status'], old['status'])} → {STATUSES.get(t.get('status'), t.get('status'))}")
    if (old["winner"] or "") != winner_key(t):
        names = ", ".join(w.get("name", "") for w in winners(t)) or "немає"
        changes.append(f"переможець: {names}")
    if not changes:
        return
    chats = {c for s in con.execute(
        "SELECT s.chat_ids FROM matches m JOIN searches s ON s.id=m.search_id "
        "WHERE m.tender_id=? AND s.enabled=1 AND s.send_changes=1", (t["id"],))
        for c in (json.loads(s[0]) or config.TG_CHAT_IDS)}
    if chats:
        tg.notify_change(t, chats, f"🔄 <b>{t.get('tenderID')}</b> змінився\n" + "\n".join(changes)
                         + f"\nhttps://prozorro.gov.ua/tender/{t.get('tenderID')}")


async def detail_worker(con, api, tg, queue):
    while True:
        item = await queue.get()
        try:
            while True:
                try:
                    await process(con, tg, await api.tender(item["id"]))
                    break
                except httpx.HTTPStatusError as e:  # 404 и т.п. — тендер не отдаётся, пропускаем
                    db.log_error(con, f"{item.get('tenderID')}: HTTP {e.response.status_code}")
                    break
                except RuntimeError as e:  # API лежит — не теряем тендер, ждём и пробуем снова
                    db.log_error(con, str(e))
                    await asyncio.sleep(30)
        except Exception as e:
            log.exception("обработка %s", item.get("tenderID"))
            db.log_error(con, f"{item.get('tenderID')}: {e}")
        finally:
            queue.task_done()


async def feed_watcher(con, api, queue):
    offset = db.get_state(con, "feed_offset")
    if not offset:
        offset = await api.head_offset()  # первый запуск: встаём на конец ленты, историю не качаем
        db.set_state(con, "feed_offset", offset)
        log.info("старт с конца ленты: %s", offset)
    while True:
        page = await api.feed(offset)
        items = page.get("data") or []
        if not items:
            db.set_state(con, "lag_sec", 0)
            await asyncio.sleep(config.FEED_IDLE_SEC)
            continue
        searches, matched = db.searches(con, enabled_only=True), db.matched_ids(con)
        for it in items:
            if wanted(it, searches, matched):
                queue.put_nowait(it)
        await queue.join()  # курсор двигаем только после обработки страницы — рестарт ничего не теряет
        offset = page["next_page"]["offset"]
        last = items[-1]["dateModified"]
        db.set_state(con, "feed_offset", offset)
        db.set_state(con, "last_modified", last)
        db.set_state(con, "lag_sec", round(time.time() - ts(last), 1))


async def backfill(con, api, queue, days):
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).timestamp()
    searches, matched = db.searches(con, enabled_only=True), db.matched_ids(con)
    offset, seen, picked, pages = None, set(), [], 0
    while True:
        page = await api.feed(offset, descending=True)
        pages += 1
        items = page.get("data") or []
        for it in items:
            if it["id"] not in seen and wanted(it, searches, matched):
                picked.append(it)
            seen.add(it["id"])
        if pages % 50 == 0:
            log.info("бэкфил: %d страниц, дошли до %s, отобрано %d", pages, items[-1]["dateModified"] if items else "-", len(picked))
        if not items or ts(items[-1]["dateModified"]) < cutoff or "next_page" not in page:
            break
        offset = page["next_page"]["offset"]
    log.info("бэкфил: просмотрено %d изменений, деталей запросим %d", len(seen), len(picked))
    for it in reversed(picked):  # от старых к новым
        if ts(it["dateModified"]) >= cutoff:
            queue.put_nowait(it)
    await queue.join()


async def run(args):
    con = db.connect()
    api = Api(on_error=lambda text: db.log_error(con, text))
    tg = Tg(con)
    queue = asyncio.Queue()
    tasks = [asyncio.create_task(tg.sender())]
    tasks += [asyncio.create_task(detail_worker(con, api, tg, queue)) for _ in range(config.DETAIL_WORKERS)]
    try:
        if args[:1] == ["backfill"]:
            await backfill(con, api, queue, float(args[1]) if len(args) > 1 else 7)
            await tg.queue.join()
        else:
            tasks += [asyncio.create_task(tg.poller())]
            await feed_watcher(con, api, queue)
    finally:
        for t in tasks:
            t.cancel()
        await api.aclose()
        await tg.http.aclose()
        log.info("остановлен, курсор: %s", db.get_state(con, "feed_offset"))


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    if not config.TG_TOKEN or not config.TG_CHAT_IDS:
        sys.exit("Задайте TG_TOKEN и TG_CHAT_IDS в .env")
    loop = asyncio.new_event_loop()
    task = loop.create_task(run(sys.argv[1:]))
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, task.cancel)  # аккуратная остановка: курсор уже сохранён постранично
    try:
        loop.run_until_complete(task)
    except asyncio.CancelledError:
        pass
