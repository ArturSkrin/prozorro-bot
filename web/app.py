"""Веб-морда: Flask + Jinja. Слушает 127.0.0.1, вход по паролю из .env, CSRF на всех POST."""
import hmac
import json
import os
import secrets
import time

from flask import Flask, abort, g, redirect, render_template, request, session, url_for

from bot import config, db  # noqa: F401  (config грузит .env)
from bot.match import DEFAULT_PARAMS, STATUSES, bid_guarantee, has_contract_guarantee, winners

app = Flask(__name__)
app.secret_key = os.environ.get("WEB_SECRET") or secrets.token_hex(32)
app.config.update(SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE="Lax")
PASSWORD = os.environ.get("WEB_PASSWORD", "")
LIST_KEYS = ["statuses", "method_types", "cpv", "regions", "keywords", "minus_words", "edrpou_allow", "edrpou_deny"]


def con():
    if "con" not in g:
        g.con = db.connect()
    return g.con


@app.teardown_appcontext
def close(_):
    if "con" in g:
        g.con.close()


@app.template_filter("money")
def money(x):
    return "—" if x is None else f"{x:,.2f}".replace(",", " ")


@app.template_filter("dt")
def dt(x):
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(x)) if x else "—"


app.jinja_env.globals.update(STATUSES=STATUSES, csrf=lambda: session.setdefault("csrf", secrets.token_hex(16)))


@app.before_request
def guard():
    if request.method == "POST":
        token = request.form.get("csrf", "")
        if not hmac.compare_digest(token, session.get("csrf", "")):
            abort(400, "CSRF")
    if request.endpoint not in ("login", "static") and not session.get("ok"):
        return redirect(url_for("login"))


@app.route("/login", methods=["GET", "POST"])
def login():
    error = None
    if request.method == "POST":
        if PASSWORD and hmac.compare_digest(request.form.get("password", ""), PASSWORD):
            session["ok"] = True
            return redirect(url_for("found"))
        time.sleep(1)  # замедляем перебор
        error = "Невірний пароль" if PASSWORD else "WEB_PASSWORD не задано в .env"
    return render_template("login.html", error=error)


@app.post("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


@app.route("/")
def found():
    q, sid, state = request.args.get("q", "").strip(), request.args.get("search", ""), request.args.get("state", "")
    sql = ("SELECT t.id, t.tender_id, t.status, t.amount, t.currency, json_extract(t.json,'$.title') title, "
           "json_extract(t.json,'$.procuringEntity.name') buyer, group_concat(s.name, ', ') searches, "
           "max(m.sent_at) sent_at, max(m.state) state, max(m.taken_by) taken_by "
           "FROM matches m JOIN tenders t ON t.id=m.tender_id LEFT JOIN searches s ON s.id=m.search_id WHERE 1=1")
    args = []
    if q:
        sql += " AND t.tender_id LIKE ?"
        args.append(f"%{q}%")
    if sid:
        sql += " AND m.search_id=?"
        args.append(int(sid))
    if state:
        sql += " AND m.state=?"
        args.append(state)
    rows = con().execute(sql + " GROUP BY t.id ORDER BY sent_at DESC LIMIT 500", args).fetchall()
    return render_template("found.html", rows=rows, searches=db.searches(con()), q=q, sid=sid, state=state)


@app.route("/tender/<tid>")
def tender(tid):
    row = con().execute("SELECT * FROM tenders WHERE id=?", (tid,)).fetchone() or abort(404)
    t = json.loads(row["json"])
    ms = con().execute("SELECT m.*, s.name FROM matches m LEFT JOIN searches s ON s.id=m.search_id WHERE tender_id=?",
                       (tid,)).fetchall()
    return render_template("tender.html", t=t, matches=ms, winners=winners(t), bg=bid_guarantee(t),
                           cg=has_contract_guarantee(t), raw=json.dumps(t, ensure_ascii=False, indent=2))


@app.post("/tender/<tid>/state")
def tender_state(tid):
    st = request.form.get("state")
    if st not in ("new", "taken", "skip"):
        abort(400)
    con().execute("UPDATE matches SET state=?, taken_by=? WHERE tender_id=?",
                  (st, "веб" if st == "taken" else None, tid))
    return redirect(url_for("tender", tid=tid))


@app.route("/searches")
def searches():
    return render_template("searches.html", searches=db.searches(con()))


@app.route("/searches/new", methods=["GET", "POST"])
@app.route("/searches/<int:sid>", methods=["GET", "POST"])
def search_edit(sid=None):
    s = db.get_search(con(), sid) if sid else {"name": "", "enabled": 1, "params": DEFAULT_PARAMS, "chat_ids": [],
                                                 "send_changes": 1}
    if s is None:
        abort(404)
    error = None
    if request.method == "POST":
        f = request.form
        try:
            params = {k: [x.strip() for x in f.get(k, "").split(",") if x.strip()] for k in LIST_KEYS if k != "statuses"}
            params["statuses"] = [x for x in f.getlist("statuses") if x in STATUSES]
            for k in ("amount_min", "amount_max"):
                v = f.get(k, "").replace(" ", "").replace(",", ".")
                params[k] = float(v) if v else None
            params["currency"] = f.get("currency", "").strip().upper() or None
            params["bid_guarantee"] = "bid_guarantee" in f
            params["contract_guarantee"] = "contract_guarantee" in f
            chat_ids = [int(x) for x in f.get("chat_ids", "").replace(" ", "").split(",") if x]
            name = f.get("name", "").strip()
            if not name:
                raise ValueError("назва обов'язкова")
        except ValueError as e:
            error = f"Помилка: {e}"
        else:
            db.save_search(con(), name, params, sid, "enabled" in f, chat_ids, "send_changes" in f)
            return redirect(url_for("searches"))
    return render_template("search_edit.html", s=s, p={**DEFAULT_PARAMS, **s["params"]}, error=error, list_keys=LIST_KEYS)


@app.post("/searches/<int:sid>/toggle")
def search_toggle(sid):
    s = db.get_search(con(), sid) or abort(404)
    db.save_search(con(), s["name"], s["params"], sid, not s["enabled"], s["chat_ids"], s["send_changes"])
    return redirect(url_for("searches"))


@app.post("/searches/<int:sid>/delete")
def search_delete(sid):
    db.delete_search(con(), sid)
    return redirect(url_for("searches"))


@app.route("/status")
def status():
    c = con()
    day = time.time() - 86400
    info = {k: db.get_state(c, k) for k in ("feed_offset", "last_modified", "lag_sec", "paused")}
    return render_template("status.html", info=info,
                           sent_day=c.execute("SELECT COUNT(*) FROM matches WHERE sent_at > ?", (day,)).fetchone()[0],
                           sent_total=c.execute("SELECT COUNT(*) FROM matches").fetchone()[0],
                           errors=c.execute("SELECT * FROM errors ORDER BY ts DESC LIMIT 50").fetchall())


if __name__ == "__main__":
    app.run(host=os.environ.get("WEB_HOST", "127.0.0.1"), port=int(os.environ.get("WEB_PORT", "8080")))
