"""Чистая логика фильтров: одна модель поиска для Telegram и веба."""
import time
from datetime import datetime

# Коды статусов API → названия как на prozorro.gov.ua
STATUSES = {
    "active.enquiries": "Період уточнень",
    "active.tendering": "Подання пропозицій",
    "active.pre-qualification": "Прекваліфікація",
    "active.pre-qualification.stand-still": "Прекваліфікація (період оскаржень)",
    "active.auction": "Аукціон",
    "active.qualification": "Кваліфікація переможця",
    "active.awarded": "Пропозиції розглянуті",
    "active": "Активна",
    "complete": "Завершена",
    "unsuccessful": "Не відбулась",
    "cancelled": "Відмінена",
}

BID_GUARANTEE = "CRITERION.OTHER.BID.GUARANTEE"
CONTRACT_GUARANTEE = "CRITERION.OTHER.CONTRACT.GUARANTEE"

# Все параметры поиска (None/пусто = не фильтровать). Тот же набор полей в TG-мастере и в вебе.
DEFAULT_PARAMS = {
    "statuses": [],          # коды API
    "amount_min": None,
    "amount_max": None,
    "currency": None,        # "UAH"
    "bid_guarantee": False,  # требовать забезпечення пропозицій
    "contract_guarantee": False,
    "method_types": [],      # procurementMethodType
    "cpv": [],               # префиксы CPV
    "regions": [],           # procuringEntity.address.region, подстрока без учёта регистра
    "keywords": [],
    "minus_words": [],
    "edrpou_allow": [],
    "edrpou_deny": [],
    "max_age_days": None,    # статус змінено не раніше ніж N днів тому (поле date)
}

DEFAULT_SEARCH = {
    "name": "Розглянуті від 3 млн, обидва забезпечення",
    "params": {**DEFAULT_PARAMS, "statuses": ["active.awarded"], "amount_min": 3_000_000,
               "currency": "UAH", "bid_guarantee": True, "contract_guarantee": True},
}


def criteria_ids(tender):
    return {(c.get("classification") or {}).get("id") for c in tender.get("criteria") or []}


def bid_guarantee(tender):
    """Сумма забезпечення пропозицій (по тендеру или сумма по лотам) или None, если его нет."""
    amount = (tender.get("guarantee") or {}).get("amount") or 0
    if not amount:
        amount = sum((l.get("guarantee") or {}).get("amount") or 0 for l in tender.get("lots") or [])
    if amount > 0:
        return amount
    return 0 if BID_GUARANTEE in criteria_ids(tender) else None  # 0 = критерий есть, сумма не указана


def has_contract_guarantee(tender):
    return CONTRACT_GUARANTEE in criteria_ids(tender)


def winners(tender):
    return [s for a in tender.get("awards") or [] if a.get("status") == "active" for s in a.get("suppliers") or []]


def prefilter(item, params):
    """Проверка по полям из ленты (status, procurementMethodType) до запроса деталей."""
    if params["statuses"] and item.get("status") not in params["statuses"]:
        return False
    if params["method_types"] and item.get("procurementMethodType") not in params["method_types"]:
        return False
    return True


def match(tender, search):
    """-> (подходит, причины). Причины — что совпало (если подходит) или что не совпало (если нет)."""
    p = {**DEFAULT_PARAMS, **search["params"]}
    ok, why = [], []

    def check(cond, good, bad):
        (ok if cond else why).append(good if cond else bad)

    if p["statuses"]:
        s = tender.get("status")
        check(s in p["statuses"], f"статус: {STATUSES.get(s, s)}", f"статус {s}")
    if p["method_types"]:
        m = tender.get("procurementMethodType")
        check(m in p["method_types"], f"процедура: {m}", f"процедура {m}")

    value = tender.get("value") or {}
    amount, cur = value.get("amount"), value.get("currency")
    if p["currency"]:
        check(cur == p["currency"], f"валюта {cur}", f"валюта {cur}")
    if p["amount_min"] is not None:
        check(amount is not None and amount >= p["amount_min"], f"сума ≥ {p['amount_min']:,.0f}", f"сума {amount}")
    if p["amount_max"] is not None:
        check(amount is not None and amount <= p["amount_max"], f"сума ≤ {p['amount_max']:,.0f}", f"сума {amount}")

    if p["max_age_days"] is not None:
        d = tender.get("date")
        age = (time.time() - datetime.fromisoformat(d).timestamp()) / 86400 if d else None
        check(age is not None and age <= p["max_age_days"], f"статус змінено {age:.1f} дн. тому" if age is not None else "",
              f"статус змінено {age:.0f} дн. тому" if age is not None else "немає дати")

    if p["bid_guarantee"]:
        check(bid_guarantee(tender) is not None, "є забезпечення пропозицій", "немає забезпечення пропозицій")
    if p["contract_guarantee"]:
        check(has_contract_guarantee(tender), "є забезпечення договору", "немає забезпечення договору")

    if p["cpv"]:
        codes = [(i.get("classification") or {}).get("id", "") for i in tender.get("items") or []]
        hit = next((c for c in codes if any(c.startswith(x) for x in p["cpv"])), None)
        check(hit is not None, f"CPV {hit}", "CPV не збігся")

    pe = tender.get("procuringEntity") or {}
    if p["regions"]:
        region = ((pe.get("address") or {}).get("region") or "").lower()
        check(any(r.lower() in region for r in p["regions"]), f"регіон {region}", f"регіон {region}")
    edrpou = (pe.get("identifier") or {}).get("id")
    if p["edrpou_allow"]:
        check(edrpou in p["edrpou_allow"], f"замовник {edrpou} у білому списку", f"замовник {edrpou} не в білому списку")
    if p["edrpou_deny"]:
        check(edrpou not in p["edrpou_deny"], "замовник не в чорному списку", f"замовник {edrpou} у чорному списку")

    title = (tender.get("title") or "").lower()
    if p["keywords"]:
        hit = next((k for k in p["keywords"] if k.lower() in title), None)
        check(hit is not None, f"слово «{hit}»", "немає ключових слів")
    if p["minus_words"]:
        hit = next((k for k in p["minus_words"] if k.lower() in title), None)
        check(hit is None, "без мінус-слів", f"мінус-слово «{hit}»")

    return (not why, ok if not why else why)
