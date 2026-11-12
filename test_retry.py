
import os
import tempfile
import uuid
import threading
from pathlib import Path
from decimal import Decimal
from concurrent.futures import ThreadPoolExecutor

# Configure test environment BEFORE importing the application.
os.environ["GUARDRAIL_DB_PATH"] = str(
    Path(tempfile.gettempdir()) /
    f"retry_test_import_{uuid.uuid4().hex}.db"
)
os.environ["AGENT_API_TOKEN"] = "test-agent-token"
os.environ["HUMAN_APPROVAL_TOKEN"] = "test-human-token"

from fastapi.testclient import TestClient
from gateway import main
from gateway import policy


def setup_test(tmp_path, monkeypatch, name="retry_test.db"):
    """Create an isolated database and a test client."""
    test_db = tmp_path / name
    monkeypatch.setattr(main.db, "DB_PATH", str(test_db))
    main.db.init()
    return TestClient(main.app)


def payment_payload(amount="10.00", merchant="coffee-shop",
                    currency="USD"):
    return {
        "agent_id": "test-agent",
        "amount": amount,
        "currency": currency,
        "merchant": merchant,
        "description": "Retry test payment"
    }


def agent_headers(key):
    return {
        "Authorization": "Bearer test-agent-token",
        "Idempotency-Key": key
    }


def human_headers():
    return {
        "Authorization": "Bearer test-human-token"
    }


def successful_order(order_id="TEST-ORDER"):
    return {
        "id": order_id,
        "approve_url": "https://example.com/approve",
        "status": "CREATED"
    }


# --------------------------------------------------
# 1. RETRY AFTER PREFLIGHT FAILURE
# --------------------------------------------------

def test_retry_after_preflight_failure(tmp_path, monkeypatch):
    client = setup_test(tmp_path, monkeypatch)

    request_ids = []
    attempt_count = 0

    def fake_create_order(amount, currency, description, request_id):
        nonlocal attempt_count
        attempt_count += 1
        request_ids.append(request_id)

        if attempt_count == 1:
            raise main.pp.PayPalPreflightError(
                "Simulated preflight failure"
            )

        return successful_order("TEST-ORDER-123")

    monkeypatch.setattr(main.pp, "create_order", fake_create_order)

    initial = client.post(
        "/evaluate",
        headers=agent_headers("retry-test-001"),
        json=payment_payload()
    )

    assert initial.status_code == 200
    tx = initial.json()

    assert tx["status"] == "create_retryable"

    spent, reserved = main.db.budget_totals()
    assert spent == 0
    assert reserved == 10

    retry = client.post(
        f"/transactions/{tx['id']}/retry-create",
        headers=human_headers()
    )

    assert retry.status_code == 200
    assert retry.json()["status"] == "awaiting_buyer"
    assert retry.json()["order_id"] == "TEST-ORDER-123"

    assert attempt_count == 2
    assert request_ids[0] == request_ids[1] == tx["id"]

    spent, reserved = main.db.budget_totals()
    assert spent == 0
    assert reserved == 10


# --------------------------------------------------
# 2. CONCURRENT RETRIES CREATE ONLY ONE ORDER
# --------------------------------------------------

def test_concurrent_retry_only_creates_one_order(tmp_path, monkeypatch):
    client = setup_test(tmp_path, monkeypatch)

    calls = []
    retry_started = threading.Event()
    release_retry = threading.Event()

    def fake_create_order(amount, currency, description, request_id):
        calls.append(request_id)

        if len(calls) == 1:
            raise main.pp.PayPalPreflightError("First attempt failed")

        retry_started.set()
        assert release_retry.wait(timeout=5)

        return successful_order("TEST-CONCURRENT-ORDER")

    monkeypatch.setattr(main.pp, "create_order", fake_create_order)

    initial = client.post(
        "/evaluate",
        headers=agent_headers("concurrent-retry-001"),
        json=payment_payload()
    )

    assert initial.status_code == 200
    tx_id = initial.json()["id"]
    assert initial.json()["status"] == "create_retryable"

    def first_retry():
        return client.post(
            f"/transactions/{tx_id}/retry-create",
            headers=human_headers()
        )

    try:
        with ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(first_retry)

            assert retry_started.wait(timeout=5)

            second = client.post(
                f"/transactions/{tx_id}/retry-create",
                headers=human_headers()
            )

            assert second.status_code == 409

            release_retry.set()
            first = future.result(timeout=5)

        assert first.status_code == 200
        assert first.json()["status"] == "awaiting_buyer"
        assert first.json()["order_id"] == "TEST-CONCURRENT-ORDER"

        assert len(calls) == 2
        assert calls[0] == calls[1] == tx_id

        spent, reserved = main.db.budget_totals()
        assert spent == 0
        assert reserved == 10

    finally:
        release_retry.set()


# --------------------------------------------------
# 3. UNKNOWN PAYPAL OUTCOME BLOCKS ANOTHER RETRY
# --------------------------------------------------

def test_unknown_paypal_outcome_blocks_another_retry(
    tmp_path, monkeypatch
):
    client = setup_test(tmp_path, monkeypatch)
    calls = []

    def fake_create_order(amount, currency, description, request_id):
        calls.append(request_id)

        if len(calls) == 1:
            raise main.pp.PayPalPreflightError("Preflight failed")

        raise main.pp.PayPalOutcomeUnknown(
            "PayPal may have received the request"
        )

    monkeypatch.setattr(main.pp, "create_order", fake_create_order)

    initial = client.post(
        "/evaluate",
        headers=agent_headers("unknown-retry-001"),
        json=payment_payload()
    )

    tx_id = initial.json()["id"]
    assert initial.json()["status"] == "create_retryable"

    retry = client.post(
        f"/transactions/{tx_id}/retry-create",
        headers=human_headers()
    )

    assert retry.status_code == 200
    assert retry.json()["status"] == "create_unknown"

    spent, reserved = main.db.budget_totals()
    assert spent == 0
    assert reserved == 10

    another_retry = client.post(
        f"/transactions/{tx_id}/retry-create",
        headers=human_headers()
    )

    assert another_retry.status_code == 409
    assert len(calls) == 2


# --------------------------------------------------
# 4. DEFINITIVE PAYPAL REJECTION RELEASES RESERVATION
# --------------------------------------------------

def test_definitive_paypal_rejection_releases_reservation(
    tmp_path, monkeypatch
):
    client = setup_test(tmp_path, monkeypatch)
    calls = []

    def fake_create_order(amount, currency, description, request_id):
        calls.append(request_id)

        if len(calls) == 1:
            raise main.pp.PayPalPreflightError("Preflight failed")

        raise main.pp.PayPalDefinitiveError(
            "PayPal explicitly rejected the order"
        )

    monkeypatch.setattr(main.pp, "create_order", fake_create_order)

    initial = client.post(
        "/evaluate",
        headers=agent_headers("reject-retry-001"),
        json=payment_payload()
    )

    tx_id = initial.json()["id"]
    assert initial.json()["status"] == "create_retryable"

    retry = client.post(
        f"/transactions/{tx_id}/retry-create",
        headers=human_headers()
    )

    assert retry.status_code == 200
    assert retry.json()["status"] == "failed"

    spent, reserved = main.db.budget_totals()
    assert spent == 0
    assert reserved == 0

    another_retry = client.post(
        f"/transactions/{tx_id}/retry-create",
        headers=human_headers()
    )

    assert another_retry.status_code == 409
    assert len(calls) == 2


# --------------------------------------------------
# 5. SAME IDEMPOTENCY KEY DOES NOT CREATE DUPLICATES
# --------------------------------------------------

def test_same_idempotency_key_does_not_create_duplicate_order(
    tmp_path, monkeypatch
):
    client = setup_test(tmp_path, monkeypatch)
    calls = []

    def fake_create_order(amount, currency, description, request_id):
        calls.append(request_id)
        return successful_order("TEST-IDEMPOTENT-ORDER")

    monkeypatch.setattr(main.pp, "create_order", fake_create_order)

    first = client.post(
        "/evaluate",
        headers=agent_headers("same-payment-001"),
        json=payment_payload()
    )

    second = client.post(
        "/evaluate",
        headers=agent_headers("same-payment-001"),
        json=payment_payload()
    )

    assert first.status_code == 200
    assert second.status_code == 200

    assert first.json()["id"] == second.json()["id"]
    assert first.json()["status"] == "awaiting_buyer"
    assert second.json()["status"] == "awaiting_buyer"

    assert len(calls) == 1

    spent, reserved = main.db.budget_totals()
    assert spent == 0
    assert reserved == 10


# --------------------------------------------------
# 6. SAME IDEMPOTENCY KEY WITH DIFFERENT AMOUNT
# --------------------------------------------------

def test_same_idempotency_key_with_different_amount_returns_409(
    tmp_path, monkeypatch
):
    client = setup_test(tmp_path, monkeypatch)
    calls = []

    def fake_create_order(amount, currency, description, request_id):
        calls.append(request_id)
        return successful_order()

    monkeypatch.setattr(main.pp, "create_order", fake_create_order)

    first = client.post(
        "/evaluate",
        headers=agent_headers("different-amount-001"),
        json=payment_payload("10.00")
    )

    second = client.post(
        "/evaluate",
        headers=agent_headers("different-amount-001"),
        json=payment_payload("20.00")
    )

    assert first.status_code == 200
    assert second.status_code == 409

    assert len(calls) == 1

    spent, reserved = main.db.budget_totals()
    assert spent == 0
    assert reserved == 10


# --------------------------------------------------
# 7. DAILY BUDGET BLOCKS PAYMENT AFTER BUDGET IS FULL
# --------------------------------------------------

def test_daily_budget_blocks_payment_after_budget_is_full(
    tmp_path, monkeypatch
):
    client = setup_test(tmp_path, monkeypatch)
    calls = []

    def fake_create_order(amount, currency, description, request_id):
        calls.append(request_id)
        return successful_order(f"TEST-BUDGET-{request_id}")
    monkeypatch.setattr(main.pp, "create_order", fake_create_order)

    for i in range(4):
        response = client.post(
            "/evaluate",
            headers=agent_headers(f"budget-test-{i:03d}"),
            json=payment_payload("25.00")
        )

        assert response.status_code == 200
        assert response.json()["status"] == "awaiting_buyer"

    spent, reserved = main.db.budget_totals()
    assert spent == 0
    assert reserved == 100

    blocked = client.post(
        "/evaluate",
        headers=agent_headers("budget-test-extra"),
        json=payment_payload("1.00")
    )

    assert blocked.status_code == 200
    assert blocked.json()["status"] == "blocked"

    assert len(calls) == 4

    spent, reserved = main.db.budget_totals()
    assert spent == 0
    assert reserved == 100


# --------------------------------------------------
# 8. TRANSACTION ABOVE HARD LIMIT IS BLOCKED
# --------------------------------------------------

def test_transaction_above_hard_limit_is_blocked(
    tmp_path, monkeypatch
):
    client = setup_test(tmp_path, monkeypatch)
    calls = []

    def fake_create_order(amount, currency, description, request_id):
        calls.append(request_id)
        return successful_order()

    monkeypatch.setattr(main.pp, "create_order", fake_create_order)

    response = client.post(
        "/evaluate",
        headers=agent_headers("hard-limit-001"),
        json=payment_payload("51.00")
    )

    assert response.status_code == 200
    assert response.json()["status"] == "blocked"
    assert len(calls) == 0

    spent, reserved = main.db.budget_totals()
    assert spent == 0
    assert reserved == 0


# --------------------------------------------------
# 9. UNSUPPORTED CURRENCY IS BLOCKED
# --------------------------------------------------

def test_unsupported_currency_is_blocked(tmp_path, monkeypatch):
    client = setup_test(tmp_path, monkeypatch)
    calls = []

    def fake_create_order(amount, currency, description, request_id):
        calls.append(request_id)
        return successful_order()

    monkeypatch.setattr(main.pp, "create_order", fake_create_order)

    response = client.post(
        "/evaluate",
        headers=agent_headers("currency-test-001"),
        json=payment_payload("10.00", currency="EUR")
    )

    assert response.status_code == 200
    assert response.json()["status"] == "blocked"
    assert len(calls) == 0

    spent, reserved = main.db.budget_totals()
    assert spent == 0
    assert reserved == 0


# --------------------------------------------------
# 10. PAYMENT ABOVE AUTO LIMIT WAITS FOR HUMAN APPROVAL
# --------------------------------------------------

def test_payment_above_auto_limit_waits_for_human_approval(
    tmp_path, monkeypatch
):
    client = setup_test(tmp_path, monkeypatch)
    calls = []

    def fake_create_order(amount, currency, description, request_id):
        calls.append(request_id)
        return successful_order()

    monkeypatch.setattr(main.pp, "create_order", fake_create_order)

    response = client.post(
        "/evaluate",
        headers=agent_headers("approval-queue-001"),
        json=payment_payload("30.00")
    )

    assert response.status_code == 200
    tx = response.json()

    assert tx["status"] == "pending_approval"
    assert len(calls) == 0

    queue = client.get(
        "/approvals",
        headers=human_headers()
    )

    assert queue.status_code == 200
    assert any(item["id"] == tx["id"] for item in queue.json())

    spent, reserved = main.db.budget_totals()
    assert spent == 0
    assert reserved == 30


# --------------------------------------------------
# 11. HUMAN REJECTION RELEASES RESERVATION
# --------------------------------------------------

def test_human_rejection_releases_reservation(
    tmp_path, monkeypatch
):
    client = setup_test(tmp_path, monkeypatch)
    calls = []

    def fake_create_order(amount, currency, description, request_id):
        calls.append(request_id)
        return successful_order()

    monkeypatch.setattr(main.pp, "create_order", fake_create_order)

    initial = client.post(
        "/evaluate",
        headers=agent_headers("human-reject-001"),
        json=payment_payload("30.00")
    )

    tx_id = initial.json()["id"]
    assert initial.json()["status"] == "pending_approval"

    decision = client.post(
        f"/approvals/{tx_id}/decision",
        headers=human_headers(),
        json={"approve": False}
    )

    assert decision.status_code == 200
    assert decision.json()["status"] == "rejected"

    assert len(calls) == 0

    spent, reserved = main.db.budget_totals()
    assert spent == 0
    assert reserved == 0


# --------------------------------------------------
# 12. HUMAN APPROVAL CREATES PAYPAL ORDER
# --------------------------------------------------

def test_human_approval_creates_paypal_order(
    tmp_path, monkeypatch
):
    client = setup_test(tmp_path, monkeypatch)
    calls = []

    def fake_create_order(amount, currency, description, request_id):
        calls.append(request_id)
        return successful_order("TEST-APPROVED-ORDER")

    monkeypatch.setattr(main.pp, "create_order", fake_create_order)

    initial = client.post(
        "/evaluate",
        headers=agent_headers("human-approve-001"),
        json=payment_payload("30.00")
    )

    tx_id = initial.json()["id"]
    assert initial.json()["status"] == "pending_approval"

    decision = client.post(
        f"/approvals/{tx_id}/decision",
        headers=human_headers(),
        json={"approve": True}
    )

    assert decision.status_code == 200
    assert decision.json()["status"] == "awaiting_buyer"
    assert decision.json()["order_id"] == "TEST-APPROVED-ORDER"

    assert len(calls) == 1
    assert calls[0] == tx_id

    spent, reserved = main.db.budget_totals()
    assert spent == 0
    assert reserved == 30


# --------------------------------------------------
# 13. HUMAN APPROVAL CANNOT OVERRIDE HARD LIMIT
# --------------------------------------------------

def test_human_approval_cannot_override_hard_limit(
    tmp_path, monkeypatch
):
    client = setup_test(tmp_path, monkeypatch)

    create_calls = []

    def fake_create_order(amount, currency, description, request_id):
        create_calls.append(request_id)
        return successful_order("TEST-HARD-LIMIT-ORDER")

    monkeypatch.setattr(
        main.pp, "create_order", fake_create_order
    )

    # Submit a $30 payment.
    response = client.post(
        "/evaluate",
        headers=agent_headers("approval-limit-001"),
        json=payment_payload("30.00")
    )

    assert response.status_code == 200
    tx = response.json()

    assert tx["status"] == "pending_approval"
    assert len(create_calls) == 0

    # Change the hard transaction limit to $20.
    monkeypatch.setattr(
        policy,
        "POLICY",
        policy.Policy(max_per_txn=Decimal("20.00"))
    )

    # A human attempts to approve the $30 payment.
    approval = client.post(
        f"/approvals/{tx['id']}/decision",
        headers=human_headers(),
        json={"approve": True}
    )

    assert approval.status_code == 200
    assert approval.json()["status"] == "blocked"

    # PayPal must never receive the payment.
    assert len(create_calls) == 0

    # The reservation must be released.
    spent, reserved = main.db.budget_totals()

    assert spent == 0
    assert reserved == 0
