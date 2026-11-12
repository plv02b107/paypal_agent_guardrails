
import pytest

from fastapi.testclient import TestClient

from gateway import main


def setup_test(tmp_path, monkeypatch):
    test_db = tmp_path / "reconcile_test.db"

    # Set isolated test credentials
    monkeypatch.setenv(
        "AGENT_API_TOKEN",
        "test-agent-token"
    )

    monkeypatch.setenv(
        "HUMAN_APPROVAL_TOKEN",
        "test-human-token"
    )

    monkeypatch.setattr(main.db, "DB_PATH", str(test_db))
    main.db.init()

    return TestClient(main.app)


def agent_headers(key):
    return {
        "Authorization": "Bearer test-agent-token",
        "Idempotency-Key": key,
    }


def human_headers():
    return {
        "Authorization": "Bearer test-human-token",
    }


def payment_payload(description="Reconciliation test"):
    return {
        "agent_id": "test-agent",
        "amount": "10.00",
        "currency": "USD",
        "merchant": "coffee-shop",
        "description": description,
    }


def create_awaiting_buyer_transaction(
    client,
    monkeypatch,
    key,
    description="Reconciliation test",
):
    monkeypatch.setattr(
        main.pp,
        "create_order",
        lambda **kwargs: {
            "id": f"TEST-ORDER-{key}",
            "approve_url": "https://example.com/approve",
            "status": "CREATED",
        },
    )

    response = client.post(
        "/evaluate",
        headers=agent_headers(key),
        json=payment_payload(description),
    )

    assert response.status_code == 200

    data = response.json()
    assert data["status"] == "awaiting_buyer"

    return data["id"]


def simulate_uncertain_capture(tx_id):
    started = main.db.transition(
        tx_id,
        expected_status="awaiting_buyer",
        new_status="capturing",
        event="test_capture_started",
    )

    assert started is True

    changed = main.db.transition(
        tx_id,
        expected_status="capturing",
        new_status="capture_unknown",
        event="test_capture_unknown",
    )

    assert changed is True


def reconcile(client, tx_id):
    return client.post(
        f"/transactions/{tx_id}/reconcile",
        headers=human_headers(),
    )


def test_reconcile_completed_capture(
    tmp_path,
    monkeypatch,
):
    client = setup_test(tmp_path, monkeypatch)

    tx_id = create_awaiting_buyer_transaction(
        client,
        monkeypatch,
        "reconcile-completed-001",
    )

    simulate_uncertain_capture(tx_id)

    monkeypatch.setattr(
        main.pp,
        "get_order",
        lambda order_id: {
            "id": order_id,
            "status": "COMPLETED",
        },
    )

    response = reconcile(client, tx_id)

    assert response.status_code == 200

    tx = main.db.get(tx_id)

    assert tx["status"] == "captured"
    assert tx["reserved_cents"] == 0
    assert tx["captured_at"] is not None


def test_reconcile_voided_order_releases_reservation(
    tmp_path,
    monkeypatch,
):
    client = setup_test(tmp_path, monkeypatch)

    tx_id = create_awaiting_buyer_transaction(
        client,
        monkeypatch,
        "reconcile-voided-001",
        "Voided reconciliation test",
    )

    simulate_uncertain_capture(tx_id)

    monkeypatch.setattr(
        main.pp,
        "get_order",
        lambda order_id: {
            "id": order_id,
            "status": "VOIDED",
        },
    )

    response = reconcile(client, tx_id)

    assert response.status_code == 200

    tx = main.db.get(tx_id)

    assert tx["status"] == "failed"
    assert tx["reserved_cents"] == 0


def test_reconcile_approved_order_retains_reservation(
    tmp_path,
    monkeypatch,
):
    client = setup_test(tmp_path, monkeypatch)

    tx_id = create_awaiting_buyer_transaction(
        client,
        monkeypatch,
        "reconcile-approved-001",
        "Approved reconciliation test",
    )

    simulate_uncertain_capture(tx_id)

    monkeypatch.setattr(
        main.pp,
        "get_order",
        lambda order_id: {
            "id": order_id,
            "status": "APPROVED",
        },
    )

    response = reconcile(client, tx_id)

    assert response.status_code == 200

    tx = main.db.get(tx_id)

    assert tx["status"] == "awaiting_buyer"
    assert tx["reserved_cents"] == 1000


def test_reconcile_paypal_unavailable_keeps_reservation(
    tmp_path,
    monkeypatch,
):
    client = setup_test(tmp_path, monkeypatch)

    tx_id = create_awaiting_buyer_transaction(
        client,
        monkeypatch,
        "reconcile-unavailable-001",
        "PayPal unavailable test",
    )

    simulate_uncertain_capture(tx_id)

    def fake_get_order(order_id):
        raise RuntimeError("PayPal network unavailable")

    monkeypatch.setattr(
        main.pp,
        "get_order",
        fake_get_order,
    )

    response = reconcile(client, tx_id)

    assert response.status_code >= 400

    tx = main.db.get(tx_id)

    assert tx["status"] == "capture_unknown"
    assert tx["reserved_cents"] == 1000


def test_reconcile_auth_failure_keeps_reservation(
    tmp_path,
    monkeypatch,
):
    client = setup_test(tmp_path, monkeypatch)

    tx_id = create_awaiting_buyer_transaction(
        client,
        monkeypatch,
        "reconcile-auth-001",
        "Authentication failure test",
    )

    simulate_uncertain_capture(tx_id)

    def fake_get_order(order_id):
        raise main.pp.PayPalPreflightError(
            "PayPal authentication failed"
        )

    monkeypatch.setattr(
        main.pp,
        "get_order",
        fake_get_order,
    )

    response = reconcile(client, tx_id)

    assert response.status_code >= 400

    tx = main.db.get(tx_id)

    assert tx["status"] == "capture_unknown"
    assert tx["reserved_cents"] == 1000


def test_reconcile_unknown_status_keeps_reservation(
    tmp_path,
    monkeypatch,
):
    client = setup_test(tmp_path, monkeypatch)

    tx_id = create_awaiting_buyer_transaction(
        client,
        monkeypatch,
        "reconcile-unknown-001",
        "Unknown status test",
    )

    simulate_uncertain_capture(tx_id)

    monkeypatch.setattr(
        main.pp,
        "get_order",
        lambda order_id: {
            "id": order_id,
            "status": "PENDING",
        },
    )

    response = reconcile(client, tx_id)

    assert response.status_code == 200

    tx = main.db.get(tx_id)

    assert tx["status"] == "capture_unknown"
    assert tx["reserved_cents"] == 1000


def test_reconcile_declined_order_releases_reservation(
    tmp_path,
    monkeypatch,
):
    client = setup_test(tmp_path, monkeypatch)

    tx_id = create_awaiting_buyer_transaction(
        client,
        monkeypatch,
        "reconcile-declined-001",
        "Declined reconciliation test",
    )

    simulate_uncertain_capture(tx_id)

    monkeypatch.setattr(
        main.pp,
        "get_order",
        lambda order_id: {
            "id": order_id,
            "status": "DECLINED",
        },
    )

    response = reconcile(client, tx_id)

    assert response.status_code == 200

    tx = main.db.get(tx_id)

    assert tx["status"] == "failed"
    assert tx["reserved_cents"] == 0




def test_reconcile_404_keeps_reservation(
    tmp_path,
    monkeypatch,
):
    client = setup_test(tmp_path, monkeypatch)

    tx_id = create_awaiting_buyer_transaction(
        client,
        monkeypatch,
        "reconcile-404-001",
        "PayPal 404 reconciliation test",
    )

    simulate_uncertain_capture(tx_id)

    def fake_get_order(order_id):
        raise main.pp.PayPalDefinitiveError(
            'PayPal rejected request: 404 '
            '{"name":"RESOURCE_NOT_FOUND"}'
        )

    monkeypatch.setattr(
        main.pp,
        "get_order",
        fake_get_order,
    )

    response = reconcile(client, tx_id)

    assert response.status_code == 502

    tx = main.db.get(tx_id)

    assert tx["status"] == "capture_unknown"
    assert tx["reserved_cents"] == 1000
    assert tx["captured_at"] is None
