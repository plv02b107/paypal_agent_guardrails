
import json
import os
import sqlite3
import time

from contextlib import closing
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from uuid import uuid4


DB_PATH = os.getenv("GUARDRAIL_DB_PATH", "guardrail_v2.db")

IST = timezone(timedelta(hours=5, minutes=30))


ACTIVE_STATES = (
    "pending_approval",
    "creating_order",
    "create_retryable",
    "create_unknown",
    "awaiting_buyer",
    "capturing",
    "capture_unknown",
    "capture_pending",
)

ALLOWED_TRANSITIONS = {
    "creating_order": {
        "create_retryable",
        "create_unknown",
        "failed",
        "awaiting_buyer",
    },
    "create_retryable": {
        "creating_order",
    },
    "awaiting_buyer": {
        "awaiting_buyer",
        "capturing",
        "capture_unknown",
        "capture_pending",
        "failed",
        "captured",
    },
    "capturing": {
        "awaiting_buyer",
        "capture_unknown",
        "failed",
        "captured",
        "capture_pending",
    },
    "create_unknown": {
        "create_unknown",
        "awaiting_buyer",
        "captured",
        "failed",
    },
    "capture_unknown": {
        "capture_unknown",
        "awaiting_buyer",
        "captured",
        "failed",
        "capture_pending",
    },
    "capture_pending": {
        "capture_pending",
        "awaiting_buyer",
        "captured",
        "failed",
    },
    "failed": set(),
    "captured": set(),
}


def connect():
    connection = sqlite3.connect(
        DB_PATH,
        timeout=15,
        isolation_level=None
    )

    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=ON")
    connection.execute("PRAGMA busy_timeout=15000")

    return connection


def init():
    with closing(connect()) as c:
        c.execute("PRAGMA journal_mode=WAL")

        c.execute("""
            CREATE TABLE IF NOT EXISTS transactions (
                id TEXT PRIMARY KEY,
                idempotency_key TEXT NOT NULL UNIQUE,
                request_hash TEXT NOT NULL,

                agent_id TEXT NOT NULL,
                amount_cents INTEGER NOT NULL,
                currency TEXT NOT NULL,
                merchant TEXT NOT NULL,
                description TEXT NOT NULL,

                decision TEXT NOT NULL,
                reasons_json TEXT NOT NULL,
                status TEXT NOT NULL,

                reserved_cents INTEGER NOT NULL DEFAULT 0,
                reservation_expires_at REAL,

                order_id TEXT UNIQUE,
                approve_url TEXT,
                paypal_status TEXT,
                last_error TEXT,

                created_at REAL NOT NULL,
                updated_at REAL NOT NULL,
                captured_at REAL
            )
        """)

        c.execute("""
            CREATE INDEX IF NOT EXISTS idx_transaction_status
            ON transactions(status)
        """)

        c.execute("""
            CREATE INDEX IF NOT EXISTS idx_transaction_created
            ON transactions(created_at)
        """)

        c.execute("""
            CREATE TABLE IF NOT EXISTS audit_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                transaction_id TEXT NOT NULL,
                event_type TEXT NOT NULL,
                details_json TEXT NOT NULL,
                created_at REAL NOT NULL,

                FOREIGN KEY(transaction_id)
                REFERENCES transactions(id)
            )
        """)


def convert_row(row):
    if row is None:
        return None

    data = dict(row)
    data["reasons"] = json.loads(data.pop("reasons_json"))

    return data


def audit(c, tx_id, event, details=None):
    c.execute("""
        INSERT INTO audit_events(
            transaction_id,
            event_type,
            details_json,
            created_at
        )
        VALUES (?, ?, ?, ?)
    """, (
        tx_id,
        event,
        json.dumps(details or {}),
        time.time()
    ))


def get(tx_id):
    with closing(connect()) as c:
        row = c.execute(
            "SELECT * FROM transactions WHERE id=?",
            (tx_id,)
        ).fetchone()

        return convert_row(row)


def get_by_key(key):
    with closing(connect()) as c:
        row = c.execute(
            "SELECT * FROM transactions WHERE idempotency_key=?",
            (key,)
        ).fetchone()

        return convert_row(row)


def get_by_order(order_id):
    with closing(connect()) as c:
        row = c.execute(
            "SELECT * FROM transactions WHERE order_id=?",
            (order_id,)
        ).fetchone()

        return convert_row(row)


def budget_totals(c=None):
    own_connection = c is None
    c = c or connect()

    try:
        today_start = datetime.now(IST).replace(
            hour=0,
            minute=0,
            second=0,
            microsecond=0
        ).timestamp()

        placeholders = ",".join("?" for _ in ACTIVE_STATES)

        spent = c.execute("""
            SELECT COALESCE(SUM(amount_cents), 0)
            FROM transactions
            WHERE status='captured'
            AND captured_at >= ?
        """, (today_start,)).fetchone()[0]

        reserved = c.execute(f"""
            SELECT COALESCE(SUM(reserved_cents), 0)
            FROM transactions
            WHERE status IN ({placeholders})
        """, ACTIVE_STATES).fetchone()[0]

        return (
            Decimal(spent) / 100,
            Decimal(reserved) / 100
        )

    finally:
        if own_connection:
            c.close()


def _expire_pending_in_transaction(c, now):
    rows = c.execute("""
        SELECT id
        FROM transactions
        WHERE status='pending_approval'
        AND reservation_expires_at IS NOT NULL
        AND reservation_expires_at < ?
    """, (now,)).fetchall()

    for row in rows:
        c.execute("""
            UPDATE transactions
            SET status='expired',
                reserved_cents=0,
                reservation_expires_at=NULL,
                updated_at=?
            WHERE id=?
        """, (now, row["id"]))

        audit(c, row["id"], "approval_expired")

    return len(rows)


def create_transaction(
    *,
    idempotency_key,
    request_hash,
    agent_id,
    amount_cents,
    currency,
    merchant,
    description,
    reservation_ttl=900,
):
    from .policy import evaluate

    now = time.time()
    tx_id = str(uuid4())

    with closing(connect()) as c:
        c.execute("BEGIN IMMEDIATE")

        try:
            existing = c.execute("""
                SELECT *
                FROM transactions
                WHERE idempotency_key=?
            """, (idempotency_key,)).fetchone()

            if existing:
                if existing["request_hash"] != request_hash:
                    raise ValueError(
                        "Idempotency key reused with different request details."
                    )

                c.commit()
                return convert_row(existing), False

            _expire_pending_in_transaction(c, now)

            spent, reserved = budget_totals(c)

            amount = Decimal(amount_cents) / 100

            decision, reasons = evaluate(
                amount=amount,
                currency=currency,
                merchant=merchant,
                spent_and_reserved=spent + reserved,
            )

            if decision == "BLOCK":
                status = "blocked"
                reserve = 0

            elif decision == "ASK_HUMAN":
                status = "pending_approval"
                reserve = amount_cents

            else:
                status = "creating_order"
                reserve = amount_cents

            c.execute("""
                INSERT INTO transactions (
                    id,
                    idempotency_key,
                    request_hash,
                    agent_id,
                    amount_cents,
                    currency,
                    merchant,
                    description,
                    decision,
                    reasons_json,
                    status,
                    reserved_cents,
                    reservation_expires_at,
                    created_at,
                    updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                tx_id,
                idempotency_key,
                request_hash,
                agent_id,
                amount_cents,
                currency,
                merchant,
                description,
                decision,
                json.dumps(reasons),
                status,
                reserve,
                now + reservation_ttl
                if status == "pending_approval"
                else None,
                now,
                now,
            ))

            audit(c, tx_id, "transaction_created", {
                "decision": decision,
                "status": status,
                "amount_cents": amount_cents,
            })

            c.commit()

        except Exception:
            c.rollback()
            raise

    return get(tx_id), True


def transition(
    tx_id,
    expected_status,
    new_status,
    event,
    fields=None,
    details=None,
):
    fields = fields or {}
    expected_states = (
        expected_status
        if isinstance(expected_status, tuple)
        else (expected_status,)
    )

    if new_status not in ALLOWED_TRANSITIONS:
        raise ValueError(
            f"Unknown target transaction state: {new_status}"
        )

    for state in expected_states:
        allowed = ALLOWED_TRANSITIONS.get(state, set())

        if new_status not in allowed:
            raise ValueError(
                f"Illegal transaction transition: "
                f"{state} -> {new_status}"
            )

    allowed_fields = {
        "reserved_cents",
        "reservation_expires_at",
        "order_id",
        "approve_url",
        "paypal_status",
        "last_error",
        "captured_at",
        "decision",
        "reasons_json",
    }

    if set(fields) - allowed_fields:
        raise ValueError("Invalid database update field.")

    expected = (
        expected_status
        if isinstance(expected_status, tuple)
        else (expected_status,)
    )

    with closing(connect()) as c:
        c.execute("BEGIN IMMEDIATE")

        try:
            row = c.execute("""
                SELECT status
                FROM transactions
                WHERE id=?
            """, (tx_id,)).fetchone()

            if row is None or row["status"] not in expected:
                c.rollback()
                return False

            assignments = [
                "status=?",
                "updated_at=?"
            ]

            values = [new_status, time.time()]

            for key, value in fields.items():
                assignments.append(f"{key}=?")
                values.append(value)

            values.append(tx_id)

            c.execute(
                f"""
                UPDATE transactions
                SET {", ".join(assignments)}
                WHERE id=?
                """,
                values
            )

            audit(c, tx_id, event, details)

            c.commit()
            return True

        except Exception:
            c.rollback()
            raise


def decide_approval(tx_id, approve):
    from .policy import POLICY

    now = time.time()

    with closing(connect()) as c:
        c.execute("BEGIN IMMEDIATE")

        try:
            row = c.execute("""
                SELECT *
                FROM transactions
                WHERE id=?
            """, (tx_id,)).fetchone()

            if row is None or row["status"] != "pending_approval":
                c.rollback()
                return None, "not_pending"

            if (
                row["reservation_expires_at"] is not None
                and row["reservation_expires_at"] < now
            ):
                c.execute("""
                    UPDATE transactions
                    SET status='expired',
                        reserved_cents=0,
                        reservation_expires_at=NULL,
                        updated_at=?
                    WHERE id=?
                """, (now, tx_id))

                audit(c, tx_id, "approval_expired")
                c.commit()

                return get(tx_id), "expired"

            if not approve:
                c.execute("""
                    UPDATE transactions
                    SET status='rejected',
                        reserved_cents=0,
                        reservation_expires_at=NULL,
                        updated_at=?
                    WHERE id=?
                """, (now, tx_id))

                audit(c, tx_id, "human_rejected")
                c.commit()

                return get(tx_id), "rejected"

            reasons = []

            if row["amount_cents"] > int(POLICY.max_per_txn * 100):
                reasons.append(
                    "Hard transaction limit failed at approval."
                )

            if row["currency"] not in POLICY.allowed_currencies:
                reasons.append(
                    "Currency restriction failed at approval."
                )

            spent, reserved = budget_totals(c)

            # Remove this transaction's existing reservation
            # before adding its amount back into the projection.
            own_reservation = Decimal(row["reserved_cents"]) / 100
            projected = spent + reserved - own_reservation + (
                Decimal(row["amount_cents"]) / 100
            )

            if projected > POLICY.daily_budget:
                reasons.append(
                    f"Daily budget exceeded at approval. "
                    f"Projected total: {projected}."
                )

            if reasons:
                c.execute("""
                    UPDATE transactions
                    SET status='blocked',
                        decision='BLOCK',
                        reasons_json=?,
                        reserved_cents=0,
                        reservation_expires_at=NULL,
                        updated_at=?
                    WHERE id=?
                """, (json.dumps(reasons), now, tx_id))

                audit(c, tx_id, "approval_blocked_by_policy", {
                    "reasons": reasons
                })

                c.commit()
                return get(tx_id), "blocked"

            c.execute("""
                UPDATE transactions
                SET status='creating_order',
                    reservation_expires_at=NULL,
                    updated_at=?
                WHERE id=?
            """, (now, tx_id))

            audit(c, tx_id, "human_approved")
            c.commit()

            return get(tx_id), "approved"

        except Exception:
            c.rollback()
            raise


def expire_pending():
    now = time.time()

    with closing(connect()) as c:
        c.execute("BEGIN IMMEDIATE")

        try:
            count = _expire_pending_in_transaction(c, now)
            c.commit()
            return count

        except Exception:
            c.rollback()
            raise


def list_transactions(status=None):
    with closing(connect()) as c:
        if status:
            rows = c.execute("""
                SELECT *
                FROM transactions
                WHERE status=?
                ORDER BY created_at DESC
            """, (status,)).fetchall()
        else:
            rows = c.execute("""
                SELECT *
                FROM transactions
                ORDER BY created_at DESC
            """).fetchall()

        return [convert_row(row) for row in rows]


def audit_events(tx_id=None):
    with closing(connect()) as c:
        if tx_id:
            rows = c.execute("""
                SELECT *
                FROM audit_events
                WHERE transaction_id=?
                ORDER BY id
            """, (tx_id,)).fetchall()
        else:
            rows = c.execute("""
                SELECT *
                FROM audit_events
                ORDER BY id DESC
                LIMIT 500
            """).fetchall()

        return [dict(row) for row in rows]
