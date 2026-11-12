
import os
import tempfile
import uuid
from pathlib import Path

# Configure test environment before importing the application.
os.environ["GUARDRAIL_DB_PATH"] = str(
    Path(tempfile.gettempdir()) /
    f"capture_test_import_{uuid.uuid4().hex}.db"
)
os.environ["AGENT_API_TOKEN"] = "test-agent-token"
os.environ["HUMAN_APPROVAL_TOKEN"] = "test-human-token"

from fastapi.testclient import TestClient
from gateway import main


def test_successful_payment_capture(tmp_path, monkeypatch):

    test_db = tmp_path / "capture_test.db"
    monkeypatch.setattr(main.db, "DB_PATH", str(test_db))
    main.db.init()

    capture_calls = []

    def fake_create_order(amount, currency, description, request_id):
        return {
            "id": "TEST-CAPTURE-ORDER",
            "approve_url": "https://example.com/approve",
            "status": "CREATED"
        }

    def fake_capture_order(order_id, request_id):
        capture_calls.append({
            "order_id": order_id,
            "request_id": request_id
        })
        return {"status": "COMPLETED"}

    monkeypatch.setattr(main.pp, "create_order", fake_create_order)
    monkeypatch.setattr(main.pp, "capture_order", fake_capture_order)

    client = TestClient(main.app)

    initial = client.post(
        "/evaluate",
        headers={
            "Authorization": "Bearer test-agent-token",
            "Idempotency-Key": "capture-test-001"
        },
        json={
            "agent_id": "test-agent",
            "amount": "10.00",
            "currency": "USD",
            "merchant": "coffee-shop",
            "description": "Successful capture test"
        }
    )

    assert initial.status_code == 200

    tx = initial.json()
    tx_id = tx["id"]
    order_id = tx["order_id"]

    assert tx["status"] == "awaiting_buyer"

    spent_before, reserved_before = main.db.budget_totals()

    assert spent_before == 0
    assert reserved_before == 10

    response = client.post(
        f"/orders/{order_id}/capture",
        headers={
            "Authorization": "Bearer test-agent-token"
        }
    )

    assert response.status_code == 200

    captured_tx = response.json()

    assert captured_tx["status"] == "captured"
    assert captured_tx["paypal_status"] == "COMPLETED"

    row = main.db.get(tx_id)

    assert row["reserved_cents"] == 0
    assert row["captured_at"] is not None

    spent_after, reserved_after = main.db.budget_totals()

    assert spent_after == 10
    assert reserved_after == 0

    assert len(capture_calls) == 1
    assert capture_calls[0]["order_id"] == order_id

    repeated = client.post(
        f"/orders/{order_id}/capture",
        headers={
            "Authorization": "Bearer test-agent-token"
        }
    )

    assert repeated.status_code == 200
    assert repeated.json()["status"] == "captured"
    assert len(capture_calls) == 1

    print("\nSUCCESSFUL CAPTURE TEST PASSED")
    print("Initial status:", tx["status"])
    print("Final status:", captured_tx["status"])
    print("PayPal capture calls:", len(capture_calls))
    print("Spent: $", spent_after)
    print("Reserved: $", reserved_after)


def test_capture_preflight_failure_is_retryable(tmp_path, monkeypatch):

    test_db = tmp_path / "capture_preflight.db"
    monkeypatch.setattr(main.db, "DB_PATH", str(test_db))
    main.db.init()

    capture_calls = []

    def fake_create_order(amount, currency, description, request_id):
        return {
            "id": "TEST-PREFLIGHT-ORDER",
            "approve_url": "https://example.com/approve",
            "status": "CREATED"
        }

    def fake_capture_order(order_id, request_id):
        capture_calls.append(order_id)
        raise main.pp.PayPalPreflightError(
            "Simulated PayPal authentication failure"
        )

    monkeypatch.setattr(main.pp, "create_order", fake_create_order)
    monkeypatch.setattr(main.pp, "capture_order", fake_capture_order)

    client = TestClient(main.app)

    initial = client.post(
        "/evaluate",
        headers={
            "Authorization": "Bearer test-agent-token",
            "Idempotency-Key": "capture-preflight-001"
        },
        json={
            "agent_id": "test-agent",
            "amount": "10.00",
            "currency": "USD",
            "merchant": "coffee-shop",
            "description": "Capture preflight test"
        }
    )

    assert initial.status_code == 200

    tx_id = initial.json()["id"]
    order_id = initial.json()["order_id"]

    assert initial.json()["status"] == "awaiting_buyer"

    response = client.post(
        f"/orders/{order_id}/capture",
        headers={
            "Authorization": "Bearer test-agent-token"
        }
    )

    assert response.status_code == 503

    row = main.db.get(tx_id)

    assert row["status"] == "awaiting_buyer"
    assert row["reserved_cents"] == 1000

    spent, reserved = main.db.budget_totals()

    assert spent == 0
    assert reserved == 10

    assert len(capture_calls) == 1

    print("\nCAPTURE PREFLIGHT TEST PASSED")
    print("HTTP response:", response.status_code)
    print("Final status:", row["status"])
    print("Spent: $", spent)
    print("Reserved: $", reserved)


def test_capture_unknown_outcome_blocks_duplicate_capture(
    tmp_path, monkeypatch
):

    test_db = tmp_path / "capture_unknown.db"
    monkeypatch.setattr(main.db, "DB_PATH", str(test_db))
    main.db.init()

    capture_calls = []

    def fake_create_order(amount, currency, description, request_id):
        return {
            "id": "TEST-UNKNOWN-CAPTURE",
            "approve_url": "https://example.com/approve",
            "status": "CREATED"
        }

    def fake_capture_order(order_id, request_id):
        capture_calls.append(order_id)
        raise main.pp.PayPalOutcomeUnknown(
            "Network failure. PayPal may have processed capture."
        )

    monkeypatch.setattr(main.pp, "create_order", fake_create_order)
    monkeypatch.setattr(main.pp, "capture_order", fake_capture_order)

    client = TestClient(main.app)

    initial = client.post(
        "/evaluate",
        headers={
            "Authorization": "Bearer test-agent-token",
            "Idempotency-Key": "capture-unknown-001"
        },
        json={
            "agent_id": "test-agent",
            "amount": "10.00",
            "currency": "USD",
            "merchant": "coffee-shop",
            "description": "Unknown capture test"
        }
    )

    assert initial.status_code == 200

    tx_id = initial.json()["id"]
    order_id = initial.json()["order_id"]

    assert initial.json()["status"] == "awaiting_buyer"

    response = client.post(
        f"/orders/{order_id}/capture",
        headers={
            "Authorization": "Bearer test-agent-token"
        }
    )

    assert response.status_code == 202

    row = main.db.get(tx_id)

    assert row["status"] == "capture_unknown"
    assert row["reserved_cents"] == 1000

    spent, reserved = main.db.budget_totals()

    assert spent == 0
    assert reserved == 10

    second_capture = client.post(
        f"/orders/{order_id}/capture",
        headers={
            "Authorization": "Bearer test-agent-token"
        }
    )

    assert second_capture.status_code == 409
    assert len(capture_calls) == 1

    print("\nUNKNOWN CAPTURE TEST PASSED")
    print("First response:", response.status_code)
    print("Second response:", second_capture.status_code)
    print("Final status:", row["status"])
    print("PayPal capture calls:", len(capture_calls))
    print("Spent: $", spent)
    print("Reserved: $", reserved)


def test_definitive_capture_rejection_releases_reservation(
    tmp_path, monkeypatch
):

    test_db = tmp_path / "capture_rejection.db"
    monkeypatch.setattr(main.db, "DB_PATH", str(test_db))
    main.db.init()

    capture_calls = []

    def fake_create_order(amount, currency, description, request_id):
        return {
            "id": "TEST-REJECTED-CAPTURE",
            "approve_url": "https://example.com/approve",
            "status": "CREATED"
        }

    def fake_capture_order(order_id, request_id):
        capture_calls.append(order_id)
        raise main.pp.PayPalDefinitiveError(
            "Simulated definitive capture rejection"
        )

    monkeypatch.setattr(main.pp, "create_order", fake_create_order)
    monkeypatch.setattr(main.pp, "capture_order", fake_capture_order)

    client = TestClient(main.app)

    initial = client.post(
        "/evaluate",
        headers={
            "Authorization": "Bearer test-agent-token",
            "Idempotency-Key": "capture-rejection-001"
        },
        json={
            "agent_id": "test-agent",
            "amount": "10.00",
            "currency": "USD",
            "merchant": "coffee-shop",
            "description": "Definitive rejection test"
        }
    )

    assert initial.status_code == 200

    tx_id = initial.json()["id"]
    order_id = initial.json()["order_id"]

    assert initial.json()["status"] == "awaiting_buyer"

    response = client.post(
        f"/orders/{order_id}/capture",
        headers={
            "Authorization": "Bearer test-agent-token"
        }
    )

    assert response.status_code == 502

    row = main.db.get(tx_id)

    assert row["status"] == "failed"
    assert row["reserved_cents"] == 0

    spent, reserved = main.db.budget_totals()

    assert spent == 0
    assert reserved == 0

    second_capture = client.post(
        f"/orders/{order_id}/capture",
        headers={
            "Authorization": "Bearer test-agent-token"
        }
    )

    assert second_capture.status_code == 409
    assert len(capture_calls) == 1

    print("\nDEFINITIVE CAPTURE REJECTION TEST PASSED")
    print("First response:", response.status_code)
    print("Second response:", second_capture.status_code)
    print("Final status:", row["status"])
    print("Spent: $", spent)
    print("Reserved: $", reserved)


def test_paypal_return_verifies_approval_and_captures(
    tmp_path, monkeypatch
):

    monkeypatch.setattr(
        main.db, "DB_PATH", str(tmp_path / "return.db")
    )
    main.db.init()

    monkeypatch.setattr(
        main.pp,
        "create_order",
        lambda *args, **kwargs: {
            "id": "RETURN_ORDER_123",
            "status": "CREATED",
            "approve_url": "https://paypal.test/approve"
        }
    )

    monkeypatch.setattr(
        main.pp,
        "get_order",
        lambda order_id: {
            "id": order_id,
            "status": "APPROVED"
        }
    )

    capture_calls = []

    def fake_capture(order_id, request_id):
        capture_calls.append(order_id)
        return {
            "id": order_id,
            "status": "COMPLETED"
        }

    monkeypatch.setattr(main.pp, "capture_order", fake_capture)

    client = TestClient(main.app)

    response = client.post(
        "/evaluate",
        headers={
            "Authorization": "Bearer test-agent-token",
            "Idempotency-Key": "return-test-123"
        },
        json={
            "agent_id": "test-agent",
            "amount": "10.00",
            "currency": "USD",
            "merchant": "coffee-shop",
            "description": "Coffee"
        }
    )

    assert response.status_code == 200

    order_id = response.json()["order_id"]

    result = client.get(
        "/paypal/return",
        params={"token": order_id}
    )

    assert result.status_code == 200
    assert result.json()["transaction"]["status"] == "captured"
    assert capture_calls == [order_id]


def test_paypal_cancel_does_not_capture_or_release_reservation(
    tmp_path, monkeypatch
):

    monkeypatch.setattr(
        main.db, "DB_PATH", str(tmp_path / "cancel.db")
    )
    main.db.init()

    monkeypatch.setattr(
        main.pp,
        "create_order",
        lambda *args, **kwargs: {
            "id": "CANCEL_ORDER_123",
            "status": "CREATED",
            "approve_url": "https://paypal.test/approve"
        }
    )

    capture_calls = []

    monkeypatch.setattr(
        main.pp,
        "capture_order",
        lambda *args, **kwargs: capture_calls.append(args[0])
    )

    client = TestClient(main.app)

    response = client.post(
        "/evaluate",
        headers={
            "Authorization": "Bearer test-agent-token",
            "Idempotency-Key": "cancel-test-123"
        },
        json={
            "agent_id": "test-agent",
            "amount": "10.00",
            "currency": "USD",
            "merchant": "coffee-shop",
            "description": "Coffee"
        }
    )

    assert response.status_code == 200

    order_id = response.json()["order_id"]
    tx_id = response.json()["id"]

    result = client.get(
        "/paypal/cancel",
        params={"token": order_id}
    )

    assert result.status_code == 200
    assert result.json()["transaction"]["status"] == "awaiting_buyer"

    assert main.db.get(tx_id)["reserved_cents"] == 1000
    assert capture_calls == []
