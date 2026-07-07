# Offline logic tests for the rewritten conversational layer.
# Stubs groq/dotenv/psycopg2 so no keys, real Postgres, or network are needed.
import os
import sys
import types
import sqlite3 as _real_sqlite3

os.environ["GROQ_API_KEY"] = "test"
os.environ["META_PHONE_NUMBER_ID"] = "123"
os.environ["META_ACCESS_TOKEN"] = "test"
os.environ["DATABASE_URL"] = "postgresql://fake:fake@localhost/fake"

# Stub groq before importing app (SDK may not be installed here)
fake_groq = types.ModuleType("groq")
class _FakeGroq:
    def __init__(self, api_key=None):
        self.chat = types.SimpleNamespace(completions=types.SimpleNamespace(create=None))
fake_groq.Groq = _FakeGroq
sys.modules["groq"] = fake_groq

# Stub dotenv if absent
try:
    import dotenv  # noqa
except ImportError:
    d = types.ModuleType("dotenv")
    d.load_dotenv = lambda *a, **k: None
    sys.modules["dotenv"] = d

# ---------------------------------------------------------------------------
# Stub psycopg2 with a real SQLite database underneath. app.py's own _q()
# helper already translates '?' -> '%s' for real Postgres; this fake cursor
# translates the '%s' back to '?' (plus the couple of DDL syntax differences)
# so every real SQL statement app.py sends - INSERT/UPDATE/SELECT, RETURNING
# id, ALTER TABLE ADD COLUMN - actually executes against a real embedded
# database and is checked for real, instead of being mocked away. This is
# the same "stub the network dependency, keep the real logic" approach
# already used above for groq - just applied to the DB driver.
# ---------------------------------------------------------------------------
_TEST_DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "test_fake_postgres.db")
if os.path.exists(_TEST_DB_PATH):
    os.remove(_TEST_DB_PATH)

def _translate_sql_for_sqlite(sql):
    sql = sql.replace("%s", "?")
    sql = sql.replace("SERIAL PRIMARY KEY", "INTEGER PRIMARY KEY AUTOINCREMENT")
    sql = sql.replace("ADD COLUMN IF NOT EXISTS", "ADD COLUMN")
    return sql

class _FakeCursor:
    def __init__(self, real_cursor):
        self._c = real_cursor
    def execute(self, sql, params=()):
        self._c.execute(_translate_sql_for_sqlite(sql), params)
        return self
    def fetchone(self):
        return self._c.fetchone()
    def fetchall(self):
        return self._c.fetchall()

class _FakeConnection:
    def __init__(self, real_conn):
        self._conn = real_conn
    def cursor(self):
        return _FakeCursor(self._conn.cursor())
    def commit(self):
        self._conn.commit()
    def rollback(self):
        self._conn.rollback()
    def close(self):
        self._conn.close()
    def __enter__(self):
        self._conn.__enter__()
        return self
    def __exit__(self, exc_type, exc, tb):
        return self._conn.__exit__(exc_type, exc, tb)

def _fake_connect(dsn, **kwargs):
    real_conn = _real_sqlite3.connect(_TEST_DB_PATH, timeout=10)
    real_conn.row_factory = _real_sqlite3.Row
    return _FakeConnection(real_conn)

fake_psycopg2 = types.ModuleType("psycopg2")
fake_psycopg2.connect = _fake_connect
fake_psycopg2_extras = types.ModuleType("psycopg2.extras")
fake_psycopg2_extras.RealDictCursor = object()  # only referenced as a kwarg value in app.py; unused by the fake
fake_psycopg2.extras = fake_psycopg2_extras
sys.modules["psycopg2"] = fake_psycopg2
sys.modules["psycopg2.extras"] = fake_psycopg2_extras

import app

# Never hit the network
sent = []
app.send_meta_message = lambda to, text: (sent.append((to, text)) or "wamid-test")

FAILS = []
def check(name, cond, extra=""):
    status = "PASS" if cond else "FAIL"
    print(f"[{status}] {name}" + (f" | {extra}" if extra and not cond else ""))
    if not cond:
        FAILS.append(name)

def fresh(phone="911234567890"):
    app.sessions[phone] = app.new_session()
    return app.sessions[phone], phone

def order_count():
    with app.get_db() as conn:
        row = app._q(conn, "SELECT COUNT(*) AS n FROM orders").fetchone()
        return row["n"]

def last_order_row(cols="*"):
    with app.get_db() as conn:
        return app._q(conn, f"SELECT {cols} FROM orders ORDER BY id DESC LIMIT 1").fetchone()

# 1. add_items validates + merges, cart block appended with deterministic total
s, p = fresh()
parsed = {"reply": "Add kar diya!", "actions": [
    {"type": "add_items", "category_number": "", "items": [{"name": "Paneer Tikka", "quantity": 2}]}]}
r = app.execute_agent_actions(s, p, parsed)
check("add: cart has exact item", s["cart"] == [{"name": "Paneer Tikka", "price": 230, "qty": 2}], str(s["cart"]))
check("add: reply shows code-computed total", "TOTAL: Rs460" in r, r)

# 2. merge on repeat add ("ek aur")
r = app.execute_agent_actions(s, p, {"reply": "Ek aur!", "actions": [
    {"type": "add_items", "category_number": "", "items": [{"name": "Paneer Tikka", "quantity": 1}]}]})
check("add: merges qty", s["cart"][0]["qty"] == 3 and len(s["cart"]) == 1, str(s["cart"]))

# 3. fuzzy/typo name resolves through ITEM_LOOKUP
s, p = fresh()
r = app.execute_agent_actions(s, p, {"reply": "", "actions": [
    {"type": "add_items", "category_number": "", "items": [{"name": "panner tika", "quantity": 1}]}]})
check("fuzzy: 'panner tika' -> Paneer Tikka @230", s["cart"] and s["cart"][0]["name"] == "Paneer Tikka" and s["cart"][0]["price"] == 230, str(s["cart"]))

# 4. hallucinated item is rejected, never billed
s, p = fresh()
r = app.execute_agent_actions(s, p, {"reply": "Shawarma aa raha hai!", "actions": [
    {"type": "add_items", "category_number": "", "items": [{"name": "Chicken Shawarma", "quantity": 1}]}]})
check("hallucination: cart stays empty", s["cart"] == [], str(s["cart"]))
check("hallucination: 'nahi mila' note appended", "nahi mila" in r, r)

# 5. remove_items (qty 0 = remove all) and partial remove
s, p = fresh()
app.add_to_cart(s, "Chicken Biryani", 150, 3)
r = app.execute_agent_actions(s, p, {"reply": "", "actions": [
    {"type": "remove_items", "category_number": "", "items": [{"name": "chicken biryani", "quantity": 1}]}]})
check("remove: partial decrement", s["cart"][0]["qty"] == 2, str(s["cart"]))
r = app.execute_agent_actions(s, p, {"reply": "", "actions": [
    {"type": "remove_items", "category_number": "", "items": [{"name": "Chicken Biryani", "quantity": 0}]}]})
check("remove: qty 0 removes all", s["cart"] == [], str(s["cart"]))

# 6. replace pattern: clear_cart then add (action order respected)
s, p = fresh()
app.add_to_cart(s, "Veg Roll", 50, 2)
r = app.execute_agent_actions(s, p, {"reply": "Sirf naan!", "actions": [
    {"type": "clear_cart", "category_number": "", "items": []},
    {"type": "add_items", "category_number": "", "items": [{"name": "Butter Naan", "quantity": 2}]}]})
check("replace: only new item in cart", s["cart"] == [{"name": "Butter Naan", "price": 40, "qty": 2}], str(s["cart"]))
check("replace: total Rs80", "TOTAL: Rs80" in r, r)

# 7. confirm without location -> asks for location, saves nothing
s, p = fresh()
app.add_to_cart(s, "Dal Tadka", 80, 1)
before = order_count()
r = app.execute_agent_actions(s, p, {"reply": "", "actions": [
    {"type": "confirm_order", "category_number": "", "items": []}]})
after = order_count()
check("confirm w/o location: asks for location", "location share" in r, r)
check("confirm w/o location: no order saved", before == after)

# 8. confirm with unresolved item in same turn -> does NOT place order
s, p = fresh()
s["location"] = "https://maps.google.com/?q=1,2"
app.add_to_cart(s, "Dal Tadka", 80, 1)
before = order_count()
r = app.execute_agent_actions(s, p, {"reply": "", "actions": [
    {"type": "add_items", "category_number": "", "items": [{"name": "Unicorn Curry", "quantity": 1}]},
    {"type": "confirm_order", "category_number": "", "items": []}]})
after = order_count()
check("confirm+unmatched item: blocked", before == after and "nahi mila" in r, r)

# 9. confirm with cart + location -> order saved with deterministic total, session reset, owner alerted
s, p = fresh()
s["location"] = "https://maps.google.com/?q=1,2"
app.add_to_cart(s, "Chicken Biryani", 150, 2)
app.add_to_cart(s, "Butter Naan", 40, 4)
sent.clear()
r = app.execute_agent_actions(s, p, {"reply": "Order laga rahi hoon!", "actions": [
    {"type": "confirm_order", "category_number": "", "items": []}]})
row = last_order_row("total, order_text")
check("finalize: total Rs460 in DB", row["total"] == "Rs460", str(dict(row)))
check("finalize: order_text from real cart", "Chicken Biryani x2 = Rs300" in row["order_text"], row["order_text"])
check("finalize: confirmation sent to customer text", "Order Confirmed" in r, r)
check("finalize: owner alert sent", any(to == app.OWNER_NUMBER for to, _ in sent), str(sent))
check("finalize: session reset", app.sessions[p]["cart"] == [] and app.sessions[p]["history"] == [])

# 10. show_menu / show_cart blocks + qty cap
s, p = fresh()
r = app.execute_agent_actions(s, p, {"reply": "Yeh raha menu:", "actions": [
    {"type": "show_menu", "category_number": "", "items": []}]})
check("show_menu: category list appended", "Konsi category chahiye?" in r, r)
r = app.execute_agent_actions(s, p, {"reply": "", "actions": [
    {"type": "add_items", "category_number": "", "items": [{"name": "Tawa Roti", "quantity": 99}]}]})
check("qty cap: 99 -> 20", s["cart"][0]["qty"] == 20, str(s["cart"]))

# 11. empty/no-op parse still produces a sane reply
s, p = fresh()
r = app.execute_agent_actions(s, p, {"reply": "", "actions": []})
check("empty parse: safe fallback reply", bool(r.strip()), r)

# 12. history trimming + truncation
s, p = fresh()
for i in range(30):
    app.history_append(s, "user", f"msg {i} " + "x" * 2000)
check("history: capped at MAX", len(s["history"]) == app.MAX_HISTORY_MESSAGES)
check("history: entries truncated", all(len(h["content"]) <= app.MAX_HISTORY_ENTRY_CHARS + 20 for h in s["history"]))

# 13. agent message assembly: system prompt has menu+state, history ends with user turn,
# and the call goes through real tool-calling (the Groq gpt-oss-20b fix), not response_format.
s, p = fresh()
app.history_append(s, "user", "2 chicken biryani")
app.history_append(s, "assistant", "Add kar diya!")
app.history_append(s, "user", "ek aur")
captured = {}
def fake_create_tools(**kwargs):
    captured.update(kwargs)
    class Func: arguments = '{"reply": "Theek hai!", "actions": []}'
    class ToolCall: function = Func()
    class Msg:
        content = None
        tool_calls = [ToolCall()]
    class Choice: message = Msg()
    class Comp: choices = [Choice()]
    return Comp()
app.groq_client.chat.completions.create = fake_create_tools
parsed = app.run_conversation_agent(s, "ek aur")
msgs = captured["messages"]
check("agent call: returns parsed JSON", parsed == {"reply": "Theek hai!", "actions": []}, str(parsed))
check("agent call: system contains real menu + state", "Chicken Biryani - Rs150" in msgs[0]["content"] and "CURRENT STATE" in msgs[0]["content"])
check("agent call: full history passed, ends with user msg", msgs[-1] == {"role": "user", "content": "ek aur"} and len(msgs) == 4, str([m["role"] for m in msgs]))
check("agent call: real tool-calling used (not response_format)", captured.get("tools") and captured.get("tool_choice", {}).get("function", {}).get("name") == "agent_turn", str(captured.get("tool_choice")))

# 13b. defensive fallback: if a future SDK/model answers via plain content instead of
# a tool call, run_conversation_agent must still parse it correctly.
s, p = fresh()
def fake_create_content(**kwargs):
    class Msg:
        content = '{"reply": "Plain content path!", "actions": []}'
        tool_calls = None
    class Choice: message = Msg()
    class Comp: choices = [Choice()]
    return Comp()
app.groq_client.chat.completions.create = fake_create_content
parsed2 = app.run_conversation_agent(s, "hi")
check("agent call: content-fallback path parses JSON", parsed2 == {"reply": "Plain content path!", "actions": []}, str(parsed2))

# 14. legacy fallback still works when agent returns None
s, p = fresh()
s["stage"] = "welcome"
r = app.legacy_intent_reply(s, p, "menu", "menu")
check("legacy fallback: menu reply", "Konsi category" in r, r)

# 15. Postgres migration: get_db()/_q() work end-to-end (INSERT..RETURNING id,
# ALTER TABLE ADD COLUMN IF NOT EXISTS, dict-like row access) and
# _compute_dashboard_data() never crashes, returning consistent counts.
data = app._compute_dashboard_data()
check("dashboard data: has all expected keys", set(["orders", "orders_json", "daily_list", "today_orders",
      "pending_count", "dispatched_count", "delivered_count", "total_count", "latest_order_id"]).issubset(data.keys()))
check("dashboard data: total_count matches real row count", data["total_count"] == order_count())
check("dashboard data: pending+dispatched+delivered == total_count",
      data["pending_count"] + data["dispatched_count"] + data["delivered_count"] == data["total_count"],
      str((data["pending_count"], data["dispatched_count"], data["delivered_count"], data["total_count"])))

latest = app.get_latest_order_id()
check("get_latest_order_id: matches last inserted id", latest == last_order_row("id")["id"], latest)

# 16. status update endpoint: JSON path (X-Requested-With: fetch) returns JSON, not a redirect
with app.app.test_client() as client:
    s, p = fresh()
    s["location"] = "https://maps.google.com/?q=1,2"
    app.add_to_cart(s, "Butter Naan", 40, 1)
    app.execute_agent_actions(s, p, {"reply": "", "actions": [{"type": "confirm_order", "category_number": "", "items": []}]})
    oid = last_order_row("id")["id"]
    resp = client.post(f"/order/{oid}/status", data={"status": "Dispatched"},
                        headers={"X-Requested-With": "fetch"},
                        auth=(app.DASHBOARD_USERNAME, app.DASHBOARD_PASSWORD or "x"))
    check("status update: JSON response for fetch requests", resp.status_code == 200 and resp.get_json() and resp.get_json().get("ok") is True, resp.data)
    resp2 = client.post(f"/order/{oid}/status", data={"status": "Delivered"},
                         auth=(app.DASHBOARD_USERNAME, app.DASHBOARD_PASSWORD or "x"))
    check("status update: redirect for plain form POST (no-JS path)", resp2.status_code == 303, resp2.status_code)

# 17. auth still fails closed on the new /api/dashboard_data endpoint
with app.app.test_client() as client:
    resp = client.get("/api/dashboard_data")
    check("auth: /api/dashboard_data requires login", resp.status_code == 401, resp.status_code)
    resp = client.get("/api/latest_order_id")
    check("auth: /api/latest_order_id stays public by design (unauth polling)", resp.status_code == 200, resp.status_code)

print()
if FAILS:
    print(f"{len(FAILS)} FAILED: {FAILS}")
    sys.exit(1)
print("ALL TESTS PASSED")
