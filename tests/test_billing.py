import sqlite3
import tempfile
import unittest
from pathlib import Path

import billing


class BillingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        billing.DB_PATH = str(Path(self.temp.name) / "panda-test.sqlite3")
        billing.init_billing_db()
        with sqlite3.connect(billing.DB_PATH) as conn:
            conn.execute("CREATE TABLE panda_users (user_id INTEGER PRIMARY KEY, unlimited INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL)")
            conn.execute("INSERT INTO panda_users(user_id, unlimited, created_at) VALUES(1001, 0, '2026-01-01T00:00:00+00:00')")

    def tearDown(self):
        self.temp.cleanup()

    def test_shared_daily_quota_enforces_limit(self):
        for _ in range(billing.FREE_DAILY_LIMIT):
            billing.consume_request(1001)
        with self.assertRaises(ValueError):
            billing.consume_request(1001)
        state = billing.access_state(1001)
        self.assertEqual(state["used_today"], billing.FREE_DAILY_LIMIT)
        self.assertEqual(state["remaining_today"], 0)

    def test_plan_catalog_has_requested_allowances(self):
        plans = {p["id"]: p for p in billing.plan_catalog()}
        self.assertEqual(plans["daily"]["stars"], 100)
        self.assertEqual(plans["daily"]["request_limit"], 50)
        self.assertEqual(plans["weekly"]["stars"], 399)
        self.assertEqual(plans["weekly"]["request_limit"], 500)
        self.assertEqual(plans["monthly"]["stars"], 700)
        self.assertEqual(plans["monthly"]["request_limit"], 2500)

    def test_weekly_subscription_enforces_500_request_period_quota(self):
        from datetime import datetime, timedelta, timezone
        now = datetime.now(timezone.utc)
        with sqlite3.connect(billing.DB_PATH) as conn:
            conn.execute(
                "INSERT INTO panda_subscriptions(user_id,plan_id,status,starts_at,expires_at,telegram_charge_id,created_at) VALUES(?,?,?,?,?,?,?)",
                (1001, "weekly", "active", (now-timedelta(minutes=1)).isoformat(),
                 (now+timedelta(days=7)).isoformat(), "weekly-test-charge", now.isoformat()))
        for _ in range(500):
            billing.consume_request(1001)
        with self.assertRaises(ValueError):
            billing.consume_request(1001)
        self.assertEqual(billing.access_state(1001)["remaining_requests"], 0)

    def test_failed_request_can_be_refunded(self):
        billing.consume_request(1001)
        billing.refund_request(1001)
        self.assertEqual(billing.access_state(1001)["used_today"], 0)

    def test_stars_payment_activates_verified_plan(self):
        old_price = billing.PLANS["daily"]["stars"]
        billing.PLANS["daily"]["stars"] = 25
        try:
            invoice = billing.create_pending_payment(1001, "daily")
            self.assertTrue(billing.validate_pending_payment(1001, invoice["payload"], "XTR", 25))
            self.assertFalse(billing.validate_pending_payment(1001, invoice["payload"], "USD", 25))
            active = billing.activate_paid_subscription(
                1001, invoice["payload"], "XTR", 25, "telegram-charge-test-1"
            )
            self.assertTrue(active)
            state = billing.access_state(1001)
            self.assertEqual(state["access"], "subscription")
            self.assertEqual(state["plan"], "daily")
            repeated = billing.activate_paid_subscription(
                1001, invoice["payload"], "XTR", 25, "telegram-charge-test-1"
            )
            self.assertEqual(repeated, active)
        finally:
            billing.PLANS["daily"]["stars"] = old_price


if __name__ == "__main__":
    unittest.main()
