
import re
from decimal import Decimal, InvalidOperation
from uuid import uuid4

from agent.gateway_client import submit_payment

from gateway import db
from gateway.policy import POLICY


# --------------------------------------------------
# TOOL 1: SUBMIT PAYMENT
# --------------------------------------------------

PAYMENT_TOOL = {
    "type": "function",
    "name": "submit_payment",
    "description": (
        "Prepare a payment request through the safety gateway. "
        "Use only when the user explicitly requests a payment."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "amount": {
                "type": "number",
                "description": "Payment amount"
            },
            "merchant": {
                "type": "string",
                "description": "Merchant name"
            },
            "currency": {
                "type": "string",
                "description": "Three-letter currency code"
            },
            "description": {
                "type": "string",
                "description": "Reason for payment"
            }
        },
        "required": [
            "amount", "merchant", "currency", "description"
        ],
        "additionalProperties": False
    },
    "strict": True
}


# --------------------------------------------------
# TOOL 2: CHECK BUDGET
# --------------------------------------------------

CHECK_BUDGET_TOOL = {
    "type": "function",
    "name": "check_budget",
    "description": (
        "Check the user's daily spending budget, amount spent, "
        "reserved amount and remaining available budget. "
        "Use when the user asks about their budget or available funds."
    ),
    "parameters": {
        "type": "object",
        "properties": {},
        "required": [],
        "additionalProperties": False
    },
    "strict": True
}


# --------------------------------------------------
# TOOL 3: PAYMENT HISTORY
# --------------------------------------------------

PAYMENT_HISTORY_TOOL = {
    "type": "function",
    "name": "get_payment_history",
    "description": (
        "Retrieve recent payment transaction records. "
        "Can filter by transaction status."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "limit": {
                "type": "integer",
                "description": "Number of recent records, from 1 to 20"
            },
            "status": {
                "type": ["string", "null"],
                "description": "Optional transaction status filter"
            }
        },
        "required": ["limit", "status"],
        "additionalProperties": False
    },
    "strict": True
}


# --------------------------------------------------
# TOOL 4: TRANSACTION STATUS
# --------------------------------------------------

TRANSACTION_STATUS_TOOL = {
    "type": "function",
    "name": "check_transaction_status",
    "description": (
        "Look up a specific transaction using its transaction ID. "
        "Returns its actual status from the gateway database."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "transaction_id": {
                "type": "string",
                "description": "Transaction ID"
            }
        },
        "required": ["transaction_id"],
        "additionalProperties": False
    },
    "strict": True
}


# All available tools.
TOOLS = [
    PAYMENT_TOOL,
    CHECK_BUDGET_TOOL,
    PAYMENT_HISTORY_TOOL,
    TRANSACTION_STATUS_TOOL
]


# --------------------------------------------------
# SAFE DATABASE OUTPUT
# --------------------------------------------------

def public_transaction(row):
    """Return only fields safe for the conversational agent."""

    if row is None:
        return None

    return {
        "transaction_id": row["id"],
        "amount": str(Decimal(row["amount_cents"]) / 100),
        "currency": row["currency"],
        "merchant": row["merchant"],
        "description": row["description"],
        "decision": row["decision"],
        "status": row["status"],
        "reasons": row["reasons"],
        "paypal_status": row.get("paypal_status"),
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "captured_at": row.get("captured_at")
    }


# --------------------------------------------------
# TOOL EXECUTION FUNCTIONS
# --------------------------------------------------

def check_budget():
    spent, reserved = db.budget_totals()

    available = POLICY.daily_budget - spent - reserved

    return {
        "daily_budget": str(POLICY.daily_budget),
        "spent_today": str(spent),
        "reserved": str(reserved),
        "available": str(available),
        "currency": "USD"
    }


def get_payment_history(limit=10, status=None):
    if isinstance(limit, bool) or not isinstance(limit, int):
        raise ValueError("Limit must be an integer.")

    if not 1 <= limit <= 20:
        raise ValueError("Limit must be between 1 and 20.")

    if status is not None:
        if not isinstance(status, str) or not status.strip():
            raise ValueError("Invalid transaction status.")

    rows = db.list_transactions(status=status)

    return {
        "count": min(len(rows), limit),
        "transactions": [
            public_transaction(row)
            for row in rows[:limit]
        ]
    }


def check_transaction_status(transaction_id):
    if not isinstance(transaction_id, str) or not transaction_id.strip():
        raise ValueError("A valid transaction ID is required.")

    row = db.get(transaction_id.strip())

    if row is None:
        return {
            "found": False,
            "message": "Transaction not found."
        }

    return {
        "found": True,
        "transaction": public_transaction(row)
    }


# --------------------------------------------------
# CENTRAL TOOL DISPATCHER
# --------------------------------------------------

def execute_tool(name, arguments, tool_call_id=None):
    """
    Execute a requested tool.

    Read-only tools access the local database.
    Payment requests go through the existing safety gateway.
    """

    if not isinstance(arguments, dict):
        raise ValueError("Tool arguments must be an object.")

    if name == "check_budget":
        return check_budget()

    if name == "get_payment_history":
        return get_payment_history(
            limit=arguments.get("limit", 10),
            status=arguments.get("status")
        )

    if name == "check_transaction_status":
        return check_transaction_status(
            arguments.get("transaction_id")
        )

    if name == "submit_payment":

        amount = arguments.get("amount")
        merchant = arguments.get("merchant")
        currency = arguments.get("currency")
        description = arguments.get("description", "")

        try:
            amount = Decimal(str(amount))
        except (InvalidOperation, TypeError, ValueError):
            raise ValueError("Invalid payment amount.")

        if not amount.is_finite() or amount <= 0:
            raise ValueError("Payment amount must be positive.")

        if not isinstance(merchant, str) or not merchant.strip():
            raise ValueError("Merchant cannot be empty.")

        if not isinstance(currency, str) or not re.fullmatch(
            r"[A-Za-z]{3}", currency
        ):
            raise ValueError("Currency must be a three-letter code.")

        if not isinstance(description, str):
            raise ValueError("Invalid payment description.")

        idempotency_key = (
            f"agent:{tool_call_id}"
            if tool_call_id
            else f"agent:{uuid4().hex}"
        )

        return submit_payment(
            amount=amount,
            merchant=merchant.strip(),
            currency=currency.upper(),
            description=description,
            idempotency_key=idempotency_key
        )

    raise ValueError(f"Unknown tool: {name}")
