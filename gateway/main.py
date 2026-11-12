
import hashlib
import hmac
import json
import os
import re
import time
import uuid

from decimal import Decimal
from typing import Annotated

from fastapi import (
    FastAPI,
    Header,
    HTTPException,
    Depends
)

from fastapi.security import (
    HTTPBearer,
    HTTPAuthorizationCredentials
)

from pydantic import (
    BaseModel,
    Field,
    ConfigDict,
    field_validator
)

from . import db
from . import paypal_client as pp
from .policy import POLICY


app = FastAPI(
    title="PayPal Agent Guardrail Gateway",
    version="2.0.0"
)

bearer = HTTPBearer(auto_error=False)

db.init()


# --------------------------------------------------
# 1. AUTHENTICATION
# --------------------------------------------------

def check_token(credentials, env_name):
    expected = os.getenv(env_name)

    if not expected:
        raise HTTPException(
            503,
            f"{env_name} is not configured."
        )

    supplied = credentials.credentials if credentials else ""

    if not hmac.compare_digest(supplied, expected):
        raise HTTPException(
            401,
            "Invalid or missing bearer token."
        )

    return True


def agent_auth(
    credentials: Annotated[
        HTTPAuthorizationCredentials | None,
        Depends(bearer)
    ]
):
    return check_token(credentials, "AGENT_API_TOKEN")


def human_auth(
    credentials: Annotated[
        HTTPAuthorizationCredentials | None,
        Depends(bearer)
    ]
):
    return check_token(credentials, "HUMAN_APPROVAL_TOKEN")


# --------------------------------------------------
# 2. REQUEST MODELS
# --------------------------------------------------

class PaymentRequest(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        str_strip_whitespace=True
    )

    agent_id: str = Field(
        default="demo-agent",
        min_length=1,
        max_length=80
    )

    amount: Decimal = Field(
        gt=0,
        max_digits=10,
        decimal_places=2
    )

    currency: str = Field(
        default="USD",
        min_length=3,
        max_length=3
    )

    merchant: str = Field(
        min_length=1,
        max_length=100
    )

    description: str = Field(
        default="",
        max_length=500
    )

    @field_validator("currency")
    @classmethod
    def normalize_currency(cls, value):
        return value.upper()


class ApprovalRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    approve: bool


# --------------------------------------------------
# 3. HELPERS
# --------------------------------------------------

def to_cents(amount: Decimal):
    return int(amount * 100)


def request_hash(req: PaymentRequest):
    canonical_request = {
        "agent_id": req.agent_id,
        "amount_cents": to_cents(req.amount),
        "currency": req.currency,
        "merchant": req.merchant,
        "description": req.description
    }

    raw = json.dumps(
        canonical_request,
        sort_keys=True,
        separators=(",", ":")
    )

    return hashlib.sha256(raw.encode()).hexdigest()


def public_transaction(row):
    if not row:
        return None

    result = dict(row)

    result["amount"] = (
        f"{Decimal(result.pop('amount_cents')) / 100:.2f}"
    )

    result.pop("request_hash", None)
    result.pop("idempotency_key", None)
    result.pop("reserved_cents", None)
    result.pop("reservation_expires_at", None)

    return result


# --------------------------------------------------
# 4. PAYPAL ORDER CREATION
# --------------------------------------------------

def start_paypal_order(tx_id):
    row = db.get(tx_id)

    if not row or row["status"] != "creating_order":
        return row

    try:
        result = pp.create_order(
            amount=f"{Decimal(row['amount_cents']) / 100:.2f}",
            currency=row["currency"],
            description=row["description"] or row["merchant"],
            request_id=tx_id
        )

    except pp.PayPalPreflightError as exc:
        # The order creation request was never sent.
        # Keep the reservation and allow a safe retry.
        db.transition(
            tx_id,
            expected_status="creating_order",
            new_status="create_retryable",
            event="paypal_create_preflight_failed",
            fields={
                "last_error": str(exc)[:500]
            }
        )

        return db.get(tx_id)

    except pp.PayPalOutcomeUnknown as exc:
        # PayPal may have created the order.
        # Keep the reservation.
        db.transition(
            tx_id,
            expected_status="creating_order",
            new_status="create_unknown",
            event="paypal_create_outcome_unknown",
            fields={
                "last_error": str(exc)[:500]
            }
        )

        return db.get(tx_id)

    except pp.PayPalDefinitiveError as exc:
        db.transition(
            tx_id,
            expected_status="creating_order",
            new_status="failed",
            event="paypal_create_rejected",
            fields={
                "reserved_cents": 0,
                "last_error": str(exc)[:500]
            }
        )

        return db.get(tx_id)

    except Exception as exc:
        db.transition(
            tx_id,
            expected_status="creating_order",
            new_status="create_unknown",
            event="unexpected_create_error",
            fields={
                "last_error": str(exc)[:500]
            }
        )

        return db.get(tx_id)

    changed = db.transition(
        tx_id,
        expected_status="creating_order",
        new_status="awaiting_buyer",
        event="paypal_order_created",
        fields={
            "order_id": result["id"],
            "approve_url": result["approve_url"],
            "paypal_status": result["status"],
            "last_error": None
        },
        details={
            "paypal_status": result["status"]
        }
    )

    # Another process may have changed the transaction.
    return db.get(tx_id)


# --------------------------------------------------
# 5. HEALTH CHECK
# --------------------------------------------------

@app.get("/health")
def health():
    return {
        "ok": True,
        "service": "paypal-agent-guardrail-gateway"
    }


# --------------------------------------------------
# 6. AGENT PAYMENT REQUEST
# --------------------------------------------------

@app.post("/evaluate")
def evaluate_payment(
    req: PaymentRequest,

    idempotency_key: Annotated[
        str,
        Header(alias="Idempotency-Key", min_length=8, max_length=64)
    ],

    _auth: Annotated[bool, Depends(agent_auth)]
):
    if not re.fullmatch(
        r"[A-Za-z0-9._:-]{8,64}",
        idempotency_key
    ):
        raise HTTPException(
            400,
            "Invalid Idempotency-Key format."
        )

    try:
        row, is_new = db.create_transaction(
            idempotency_key=idempotency_key,
            request_hash=request_hash(req),
            agent_id=req.agent_id,
            amount_cents=to_cents(req.amount),
            currency=req.currency,
            merchant=req.merchant,
            description=req.description
        )

    except ValueError as exc:
        raise HTTPException(409, str(exc))

    if is_new and row["status"] == "creating_order":
        row = start_paypal_order(row["id"])

    return public_transaction(row)


# --------------------------------------------------
# 7. HUMAN APPROVAL QUEUE
# --------------------------------------------------

@app.get("/approvals")
def pending_approvals(
    _auth: Annotated[bool, Depends(human_auth)]
):
    db.expire_pending()

    rows = db.list_transactions("pending_approval")

    return [
        public_transaction(row)
        for row in rows
    ]


# --------------------------------------------------
# 8. HUMAN APPROVAL / REJECTION
# --------------------------------------------------

@app.post("/approvals/{tx_id}/decision")
def decide(
    tx_id: str,
    body: ApprovalRequest,

    _auth: Annotated[bool, Depends(human_auth)]
):
    row, outcome = db.decide_approval(
        tx_id,
        body.approve
    )

    if outcome == "not_pending":
        raise HTTPException(
            409,
            "Transaction is not awaiting approval."
        )

    if outcome == "expired":
        raise HTTPException(
            409,
            "Approval expired. Reservation released."
        )

    if outcome in ("blocked", "rejected"):
        return public_transaction(row)

    row = start_paypal_order(tx_id)

    return public_transaction(row)


# --------------------------------------------------
# 9. RETRY ORDER CREATION AFTER PREFLIGHT FAILURE
# --------------------------------------------------

@app.post("/transactions/{tx_id}/retry-create")
def retry_create(
    tx_id: str,

    _auth: Annotated[bool, Depends(human_auth)]
):
    row = db.get(tx_id)

    if not row:
        raise HTTPException(
            404,
            "Transaction not found."
        )

    if row["status"] != "create_retryable":
        raise HTTPException(
            409,
            f"Order creation cannot be retried from state {row['status']}."
        )

    claimed = db.transition(
        tx_id,
        expected_status="create_retryable",
        new_status="creating_order",
        event="paypal_create_retry_started",
        fields={
            "last_error": None
        }
    )

    if not claimed:
        raise HTTPException(
            409,
            "Another request changed this transaction."
        )

    return public_transaction(
        start_paypal_order(tx_id)
    )


# --------------------------------------------------
# 10. SHARED PAYPAL CAPTURE LOGIC
# --------------------------------------------------

def capture_registered_order(order_id):
    row = db.get_by_order(order_id)

    if not row:
        raise HTTPException(
            404,
            "Order is not registered with this gateway."
        )

    if row["status"] == "captured":
        return public_transaction(row)

    if row["status"] != "awaiting_buyer":
        raise HTTPException(
            409,
            f"Order cannot be captured from state {row['status']}."
        )

    claimed = db.transition(
        row["id"],
        expected_status="awaiting_buyer",
        new_status="capturing",
        event="capture_started"
    )

    if not claimed:
        raise HTTPException(
            409,
            "Another capture or state change is in progress."
        )

    capture_request_id = str(
        uuid.uuid5(
            uuid.NAMESPACE_URL,
            f"paypal-capture:{row['id']}"
        )
    )

    try:
        result = pp.capture_order(
            order_id=order_id,
            request_id=capture_request_id
        )

    except pp.PayPalPreflightError as exc:
        db.transition(
            row["id"],
            expected_status="capturing",
            new_status="awaiting_buyer",
            event="paypal_capture_preflight_failed",
            fields={"last_error": str(exc)[:500]}
        )

        raise HTTPException(
            503,
            "PayPal authentication failed before capture. "
            "No payment operation was sent. You can retry."
        )

    except pp.PayPalOutcomeUnknown as exc:
        db.transition(
            row["id"],
            expected_status="capturing",
            new_status="capture_unknown",
            event="paypal_capture_outcome_unknown",
            fields={"last_error": str(exc)[:500]}
        )

        raise HTTPException(
            202,
            "Capture outcome unknown. Reservation retained. "
            "Reconcile before retrying."
        )

    except pp.PayPalDefinitiveError as exc:
        db.transition(
            row["id"],
            expected_status="capturing",
            new_status="failed",
            event="paypal_capture_rejected",
            fields={
                "reserved_cents": 0,
                "last_error": str(exc)[:500]
            }
        )

        raise HTTPException(
            502,
            "PayPal definitively rejected capture. "
            "Reservation released."
        )

    except Exception as exc:
        db.transition(
            row["id"],
            expected_status="capturing",
            new_status="capture_unknown",
            event="unexpected_capture_error",
            fields={"last_error": str(exc)[:500]}
        )

        raise HTTPException(
            202,
            "Unexpected capture error. "
            "Outcome must be reconciled."
        )

    paypal_status = result.get("status", "UNKNOWN")

    if paypal_status == "COMPLETED":
        new_status = "captured"
        reserved = 0
        captured_at = time.time()

    elif paypal_status in ("DECLINED", "VOIDED"):
        new_status = "failed"
        reserved = 0
        captured_at = None

    else:
        new_status = "capture_pending"
        reserved = row["amount_cents"]
        captured_at = None

    db.transition(
        row["id"],
        expected_status="capturing",
        new_status=new_status,
        event=f"paypal_capture_{new_status}",
        fields={
            "paypal_status": paypal_status,
            "reserved_cents": reserved,
            "captured_at": captured_at,
            "last_error": None
        },
        details={"paypal_status": paypal_status}
    )

    return public_transaction(db.get(row["id"]))


# --------------------------------------------------
# 10A. AUTHENTICATED AGENT CAPTURE
# --------------------------------------------------

@app.post("/orders/{order_id}/capture")
def capture(
    order_id: str,
    _auth: Annotated[bool, Depends(agent_auth)]
):
    return capture_registered_order(order_id)


# --------------------------------------------------
# 10B. PAYPAL BUYER RETURN
# --------------------------------------------------

@app.get("/paypal/return")
def paypal_return(token: str):

    if not token or len(token) > 64:
        raise HTTPException(
            400,
            "Invalid or missing PayPal order token."
        )

    row = db.get_by_order(token)

    if not row:
        raise HTTPException(
            404,
            "PayPal order is not registered with this gateway."
        )

    # A completed callback may be retried by PayPal/browser.
    if row["status"] == "captured":
        return {
            "message": "Payment was already completed.",
            "transaction": public_transaction(row)
        }

    if row["status"] != "awaiting_buyer":
        raise HTTPException(
            409,
            f"Order cannot be completed from state {row['status']}."
        )

    # Verify buyer approval directly with PayPal.
    try:
        paypal_order = pp.get_order(token)

    except pp.PayPalPreflightError:
        raise HTTPException(
            503,
            "Could not authenticate with PayPal. "
            "No capture was attempted."
        )

    except pp.PayPalOutcomeUnknown:
        raise HTTPException(
            202,
            "Could not verify PayPal order status. "
            "No capture was attempted. Retry or reconcile."
        )

    except pp.PayPalDefinitiveError:
        raise HTTPException(
            502,
            "PayPal rejected the order verification request."
        )

    if paypal_order.get("status") != "APPROVED":
        raise HTTPException(
            409,
            "PayPal order has not been approved by the buyer."
        )

    result = capture_registered_order(token)

    return {
        "message": "PayPal checkout processed.",
        "transaction": result
    }


# --------------------------------------------------
# 10C. PAYPAL BUYER CANCELLATION
# --------------------------------------------------

@app.get("/paypal/cancel")
def paypal_cancel(token: str | None = None):

    row = db.get_by_order(token) if token else None

    if row:
        return {
            "message": "Checkout cancelled by buyer.",
            "transaction": public_transaction(row),
            "note": (
                "No capture was initiated. "
                "The transaction remains available for "
                "the existing recovery or expiry workflow."
            )
        }

    return {
        "message": "Checkout cancelled by buyer.",
        "order_id": token,
        "note": "No capture was initiated."
    }




# --------------------------------------------------
# 11. RECONCILE UNCERTAIN PAYPAL OUTCOMES
# --------------------------------------------------

@app.post("/transactions/{tx_id}/reconcile")
def reconcile(
    tx_id: str,

    _auth: Annotated[bool, Depends(human_auth)]
):
    row = db.get(tx_id)

    if not row:
        raise HTTPException(
            404,
            "Transaction not found."
        )

    allowed_states = (
        "create_unknown",
        "capture_unknown",
        "capture_pending",
        "awaiting_buyer"
        )

    if row["status"] not in allowed_states:
        raise HTTPException(
            409,
            f"Transaction does not need reconciliation: {row['status']}."
        )

    if not row["order_id"]:
        raise HTTPException(
            409,
            "No PayPal order ID is available. "
            "Do not release the reservation automatically. "
            "Investigate the PayPal order using the original "
            "request ID before taking further action."
        )

    try:
        paypal_order = pp.get_order(row["order_id"])

    except pp.PayPalPreflightError:
        raise HTTPException(
            503,
            "Could not authenticate with PayPal. "
            "No reconciliation was completed."
        )

    except Exception:
        raise HTTPException(
            502,
            "Could not confirm status with PayPal. "
            "Reservation remains held."
        )

    status = paypal_order.get("status", "UNKNOWN")

    if status == "COMPLETED":
        target = "captured"
        reserved = 0
        captured_at = time.time()

    elif status in ("DECLINED", "VOIDED"):
        target = "failed"
        reserved = 0
        captured_at = None

    elif status in (
        "CREATED",
        "APPROVED",
        "PAYER_ACTION_REQUIRED"
    ):
        target = "awaiting_buyer"
        reserved = row["amount_cents"]
        captured_at = None

    else:
        target = row["status"]
        reserved = row["amount_cents"]
        captured_at = None

    changed = db.transition(
        tx_id,
        expected_status=row["status"],
        new_status=target,
        event="paypal_reconciled",
        fields={
            "paypal_status": status,
            "reserved_cents": reserved,
            "captured_at": captured_at,
            "last_error": None
        },
        details={
            "paypal_status": status
        }
    )

    # Do not return a stale state if another request won the race.
    return public_transaction(db.get(tx_id))

# --------------------------------------------------
# 12. RECOVER STALE OPERATIONS
# --------------------------------------------------

@app.post("/transactions/{tx_id}/recover-stale")
def recover_stale(
    tx_id: str,
    _auth: Annotated[bool, Depends(human_auth)]
):
    row = db.get(tx_id)

    if not row:
        raise HTTPException(404, "Transaction not found.")

    if row["status"] not in ("creating_order", "capturing"):
        raise HTTPException(
            409,
            f"Transaction is not in a recoverable state: {row['status']}."
        )

    age_seconds = time.time() - row["updated_at"]

    if age_seconds < 600:
        raise HTTPException(
            409,
            "Operation is not yet stale. Wait at least 10 minutes."
        )

    if row["status"] == "creating_order":
        target = "create_unknown"
    else:
        target = "capture_unknown"

    changed = db.transition(
        tx_id,
        expected_status=row["status"],
        new_status=target,
        event="stale_operation_marked_uncertain",
        fields={
            "last_error": (
                "Operation remained in progress for over 10 minutes. "
                "Manual PayPal reconciliation required."
            )
        },
        details={
            "previous_status": row["status"],
            "age_seconds": int(age_seconds)
        }
    )

    if not changed:
        raise HTTPException(
            409,
            "Transaction changed while recovery was being attempted."
        )

    return public_transaction(db.get(tx_id))

# --------------------------------------------------
# 12. TRANSACTION HISTORY
# --------------------------------------------------

@app.get("/transactions")
def transactions(
    _auth: Annotated[bool, Depends(human_auth)],
    status: str | None = None
):
    rows = db.list_transactions(status)

    return [
        public_transaction(row)
        for row in rows
    ]


# --------------------------------------------------
# 13. AUDIT LOG
# --------------------------------------------------

@app.get("/audit")
def audit(
    _auth: Annotated[bool, Depends(human_auth)],
    tx_id: str | None = None
):
    return db.audit_events(tx_id)


# --------------------------------------------------
# 14. CURRENT POLICY
# --------------------------------------------------

@app.get("/policy")
def get_policy(
    _auth: Annotated[bool, Depends(human_auth)]
):
    return {
        "max_per_txn": str(POLICY.max_per_txn),
        "daily_budget": str(POLICY.daily_budget),
        "auto_limits": {
            key: str(value)
            for key, value in POLICY.auto_limits.items()
        },
        "allowed_currencies": list(POLICY.allowed_currencies)
    }
