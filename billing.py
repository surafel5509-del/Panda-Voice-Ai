"""Shared account quotas and Telegram Stars subscriptions for Panda Voice AI."""
import os
import secrets
import sqlite3
from datetime import datetime, timedelta, timezone

DB_PATH = os.getenv("PANDA_DATABASE_PATH", "/data/panda.sqlite3")
FREE_DAILY_LIMIT = max(1, int(os.getenv("PANDA_DAILY_REQUEST_LIMIT", "10")))
PAID_DAILY_LIMIT = max(FREE_DAILY_LIMIT, int(os.getenv("PANDA_SUBSCRIPTION_DAILY_LIMIT", "100")))
PLANS = {
    "daily": {"name": "Daily", "days": 1, "stars": max(0, int(os.getenv("PANDA_PLAN_DAILY_STARS", "0")))},
    "weekly": {"name": "Weekly", "days": 7, "stars": max(0, int(os.getenv("PANDA_PLAN_WEEKLY_STARS", "0")))},
    "monthly": {"name": "Monthly", "days": 30, "stars": max(0, int(os.getenv("PANDA_PLAN_MONTHLY_STARS", "0")))},
}


def _now():
    return datetime.now(timezone.utc)


def _connect():
    os.makedirs(os.path.dirname(DB_PATH) or ".", exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_billing_db():
    with _connect() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS panda_usage (
                user_id INTEGER NOT NULL,
                usage_day TEXT NOT NULL,
                used INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY(user_id, usage_day)
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS panda_subscriptions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                plan_id TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'active',
                starts_at TEXT NOT NULL,
                expires_at TEXT NOT NULL,
                telegram_charge_id TEXT NOT NULL UNIQUE,
                created_at TEXT NOT NULL
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_panda_sub_user_expiry ON panda_subscriptions(user_id, status, expires_at)")
        conn.execute("""
            CREATE TABLE IF NOT EXISTS panda_pending_payments (
                payload TEXT PRIMARY KEY,
                user_id INTEGER NOT NULL,
                plan_id TEXT NOT NULL,
                expected_stars INTEGER NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                created_at TEXT NOT NULL
            )
        """)


def plan_catalog():
    return [
        {"id": key, "name": value["name"], "duration_days": value["days"],
         "stars": value["stars"], "available": value["stars"] > 0,
         "daily_request_limit": PAID_DAILY_LIMIT}
        for key, value in PLANS.items()
    ]


def create_pending_payment(user_id: int, plan_id: str):
    plan = PLANS.get(plan_id)
    if not plan or plan["stars"] <= 0:
        raise ValueError("This subscription plan is not configured yet.")
    payload = f"panda:{user_id}:{plan_id}:{secrets.token_urlsafe(18)}"
    with _connect() as conn:
        conn.execute(
            "INSERT INTO panda_pending_payments(payload,user_id,plan_id,expected_stars,created_at) VALUES(?,?,?,?,?)",
            (payload, user_id, plan_id, plan["stars"], _now().isoformat()),
        )
    return {"payload": payload, "stars": plan["stars"], "name": plan["name"], "days": plan["days"]}


def validate_pending_payment(user_id: int, payload: str, currency: str, total_amount: int):
    with _connect() as conn:
        row = conn.execute(
            "SELECT expected_stars FROM panda_pending_payments WHERE payload=? AND user_id=? AND status='pending'",
            (payload, user_id),
        ).fetchone()
    return bool(row and currency == "XTR" and int(total_amount) == int(row["expected_stars"]))


def activate_paid_subscription(user_id: int, payload: str, currency: str, total_amount: int, charge_id: str):
    now = _now()
    with _connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        already = conn.execute(
            "SELECT plan_id,expires_at FROM panda_subscriptions WHERE telegram_charge_id=?",
            (charge_id,),
        ).fetchone()
        if already:
            return {"plan_id": already["plan_id"], "expires_at": already["expires_at"]}
        pending = conn.execute(
            "SELECT * FROM panda_pending_payments WHERE payload=? AND user_id=? AND status='pending'",
            (payload, user_id),
        ).fetchone()
        if not pending or currency != "XTR" or int(total_amount) != int(pending["expected_stars"]):
            return False
        plan = PLANS.get(pending["plan_id"])
        if not plan:
            return False
        active = conn.execute(
            "SELECT expires_at FROM panda_subscriptions WHERE user_id=? AND status='active' AND expires_at>? ORDER BY expires_at DESC LIMIT 1",
            (user_id, now.isoformat()),
        ).fetchone()
        starts = max(now, datetime.fromisoformat(active["expires_at"])) if active else now
        expires = starts + timedelta(days=plan["days"])
        try:
            conn.execute(
                "INSERT INTO panda_subscriptions(user_id,plan_id,status,starts_at,expires_at,telegram_charge_id,created_at) VALUES(?,?,?,?,?,?,?)",
                (user_id, pending["plan_id"], "active", starts.isoformat(), expires.isoformat(), charge_id, now.isoformat()),
            )
        except sqlite3.IntegrityError:
            return False
        conn.execute("UPDATE panda_pending_payments SET status='paid' WHERE payload=?", (payload,))
        return {"plan_id": pending["plan_id"], "expires_at": expires.isoformat()}


def access_state(user_id: int, unlimited: bool = False):
    now = _now()
    day = now.date().isoformat()
    with _connect() as conn:
        row = conn.execute("SELECT used FROM panda_usage WHERE user_id=? AND usage_day=?", (user_id, day)).fetchone()
        used = int(row["used"]) if row else 0
        sub = conn.execute(
            "SELECT plan_id,expires_at FROM panda_subscriptions WHERE user_id=? AND status='active' AND expires_at>? ORDER BY expires_at DESC LIMIT 1",
            (user_id, now.isoformat()),
        ).fetchone()
    if unlimited:
        return {"access": "unlimited", "plan": "admin", "daily_limit": None, "used_today": used, "remaining_today": None, "expires_at": None}
    if sub:
        return {"access": "subscription", "plan": sub["plan_id"], "daily_limit": PAID_DAILY_LIMIT,
                "used_today": used, "remaining_today": max(0, PAID_DAILY_LIMIT - used), "expires_at": sub["expires_at"]}
    return {"access": "free", "plan": "free", "daily_limit": FREE_DAILY_LIMIT,
            "used_today": used, "remaining_today": max(0, FREE_DAILY_LIMIT - used), "expires_at": None}


def consume_request(user_id: int, unlimited: bool = False):
    """Atomically reserve one shared daily unit for Chat, TTS and API calls."""
    now = _now()
    day = now.date().isoformat()
    if unlimited:
        return access_state(user_id, True)
    with _connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        user = conn.execute("SELECT unlimited FROM panda_users WHERE user_id=?", (user_id,)).fetchone()
        is_unlimited = bool(user and user["unlimited"])
        sub = conn.execute(
            "SELECT plan_id,expires_at FROM panda_subscriptions WHERE user_id=? AND status='active' AND expires_at>? ORDER BY expires_at DESC LIMIT 1",
            (user_id, now.isoformat()),
        ).fetchone()
        limit = None if is_unlimited else (PAID_DAILY_LIMIT if sub else FREE_DAILY_LIMIT)
        row = conn.execute("SELECT used FROM panda_usage WHERE user_id=? AND usage_day=?", (user_id, day)).fetchone()
        used = int(row["used"]) if row else 0
        if limit is not None and used >= limit:
            raise ValueError(f"Daily limit reached ({limit} requests/day). Upgrade a plan or try again tomorrow.")
        conn.execute(
            "INSERT INTO panda_usage(user_id,usage_day,used) VALUES(?,?,1) "
            "ON CONFLICT(user_id,usage_day) DO UPDATE SET used=used+1",
            (user_id, day),
        )
    return access_state(user_id, is_unlimited)


def refund_request(user_id: int):
    day = _now().date().isoformat()
    with _connect() as conn:
        conn.execute("UPDATE panda_usage SET used=MAX(0, used-1) WHERE user_id=? AND usage_day=?", (user_id, day))


def current_subscription(user_id: int):
    now = _now().isoformat()
    with _connect() as conn:
        row = conn.execute(
            "SELECT plan_id,starts_at,expires_at FROM panda_subscriptions WHERE user_id=? AND status='active' AND expires_at>? ORDER BY expires_at DESC LIMIT 1",
            (user_id, now),
        ).fetchone()
    return dict(row) if row else None
