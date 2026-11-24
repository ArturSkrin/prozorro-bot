import asyncio
import copy
import json
from pathlib import Path

import pytest

from bot import db, main, tg
from bot.match import BID_GUARANTEE, CONTRACT_GUARANTEE, DEFAULT_SEARCH, match, prefilter

FX = Path(__file__).parent / "fixtures"
T = json.loads((FX / "awarded.json").read_text())  # реальный UA-2026-10-31-000190-a
S = {"id": 1, **DEFAULT_SEARCH}


def drop_criterion(t, cid):
    t["criteria"] = [c for c in t["criteria"] if c["classification"]["id"] != cid]


def no_guarantee_amount(t):
    t.pop("guarantee")
    for lot in t["lots"]:
        lot.pop("guarantee", None)


def search(**params):
    return {"id": 1, "name": "t", "params": params}


def test_default_search_matches():
    ok, why = match(T, S)
    assert ok, why
    assert "є забезпечення договору" in why


@pytest.mark.parametrize("params, mutate", [
    ({"statuses": ["active.awarded"]}, lambda t: t.update(status="active.tendering")),
    ({"amount_min": 3_000_000}, lambda t: t["value"].update(amount=2_000_000)),
    ({"amount_max": 1_000_000}, lambda t: None),
    ({"currency": "UAH"}, lambda t: t["value"].update(currency="EUR")),
    ({"bid_guarantee": True}, lambda t: (no_guarantee_amount(t), drop_criterion(t, BID_GUARANTEE))),
    ({"contract_guarantee": True}, lambda t: drop_criterion(t, CONTRACT_GUARANTEE)),
    ({"method_types": ["belowThreshold"]}, lambda t: None),
    ({"cpv": ["4882"]}, lambda t: t["items"][0]["classification"].update(id="09130000-9")),
    ({"regions": ["Львів"]}, lambda t: None),
    ({"keywords": ["платформ"]}, lambda t: t.update(title="Закупівля паперу")),
    ({"minus_words": ["платформ"]}, lambda t: None),
    ({"edrpou_allow": ["33240845"]}, lambda t: t["procuringEntity"]["identifier"].update(id="99999999")),
    ({"edrpou_deny": ["33240845"]}, lambda t: None),
    ({"max_age_days": 1}, lambda t: t.update(date="2026-01-01T00:00:00+02:00")),
])
def test_each_filter_rejects(params, mutate):
    t = copy.deepcopy(T)
    mutate(t)
    ok, why = match(t, search(**params))
    assert not ok and why


def test_max_age_days_fresh():
    from datetime import datetime, timezone
    t = copy.deepcopy(T)
    t["date"] = datetime.now(timezone.utc).isoformat()
    assert match(t, search(max_age_days=3))[0]


def test_bid_guarantee_by_criterion_only():
    t = copy.deepcopy(T)
    no_guarantee_amount(t)
    assert match(t, search(bid_guarantee=True))[0]


def test_real_tender_without_contract_guarantee():
    t = json.loads((FX / "no_contract_guarantee.json").read_text())  # реальный UA-2026-09-16-013905-a, 11.9 млн
    assert match(t, S) == (False, ["немає забезпечення договору"])


def test_message_has_real_contacts():
    text = tg.tender_text(T, ["x"], 3)
    assert "АНВІ-КОМ" in text and "+380443389395" in text and "zems@i.ua" in text and "330 000.00 UAH" in text


def test_prefilter():
    assert prefilter({"status": "active.awarded"}, S["params"])
    assert not prefilter({"status": "complete"}, S["params"])


class FakeTg:
    def __init__(self):
        self.sent, self.changes = [], []

    def notify(self, t, by_chat, delay):
        self.sent.append((t["tenderID"], by_chat))

    def notify_change(self, t, chats, text):
        self.changes.append(text)


class FakeApi:
    def __init__(self, pages, tenders):
        self.pages, self.tenders, self.calls = pages, tenders, []

    async def feed(self, offset=None, descending=False, limit=1000):
        self.calls.append(offset)
        return self.pages.get(offset, {"data": [], "next_page": {"offset": offset}})

    async def head_offset(self):
        return "HEAD"

    async def tender(self, tid):
        return copy.deepcopy(self.tenders[tid])


@pytest.fixture
def con(tmp_path, monkeypatch):
    monkeypatch.setattr(main.config, "TG_CHAT_IDS", [100])
    return db.connect(str(tmp_path / "t.db"))


def run_feed(con, api, tg, seconds=0.3):
    async def go():
        q = asyncio.Queue()
        w = asyncio.create_task(main.detail_worker(con, api, tg, q))
        try:
            await asyncio.wait_for(main.feed_watcher(con, api, q), seconds)
        except asyncio.TimeoutError:
            pass
        w.cancel()
    main.config.FEED_IDLE_SEC = 0.05
    asyncio.run(go())


def test_feed_cursor_dedup_and_changes(con):
    item = {k: T[k] for k in ("id", "tenderID", "status", "procurementMethodType", "dateModified")}
    skip = {**item, "id": "x", "status": "complete"}
    api = FakeApi({"HEAD": {"data": [skip, item], "next_page": {"offset": "P2"}}}, {T["id"]: T})
    tg = FakeTg()
    run_feed(con, api, tg)
    assert tg.sent == [("UA-2026-10-31-000190-a", {100: [DEFAULT_SEARCH["name"]]})]
    assert db.get_state(con, "feed_offset") == "P2"  # курсор сохранён
    assert con.execute("SELECT COUNT(*) FROM tenders").fetchone()[0] == 1  # complete не запрашивали

    # рестарт: продолжаем с P2; тот же тендер изменился (новый победитель) — повторно не шлём, шлём изменение
    changed = copy.deepcopy(T)
    changed["awards"][0]["suppliers"][0].update(name="ТОВ Новий", identifier={"id": "22222222"})
    api = FakeApi({"P2": {"data": [item], "next_page": {"offset": "P3"}}}, {T["id"]: changed})
    tg2 = FakeTg()
    run_feed(con, api, tg2)
    assert api.calls[0] == "P2"
    assert tg2.sent == [] and len(tg2.changes) == 1 and "ТОВ Новий" in tg2.changes[0]
    assert db.get_state(con, "feed_offset") == "P3"


def test_web_money_format():
    from web.app import money, money_html
    assert money(22499412000) == "22 499 412 000,00"
    assert str(money_html(13409027.45)) == '13 409 027<span class="dec">,45</span>'
    assert money(None) == "—"
