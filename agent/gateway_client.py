
import os
import re
import httpx

from dotenv import load_dotenv


# Load environment variables from the project's .env file
load_dotenv()


# Gateway configuration
GATEWAY_URL = os.getenv(
    "GATEWAY_BASE_URL",
    "http://127.0.0.1:8000"
)

AGENT_API_TOKEN = os.getenv("AGENT_API_TOKEN")


class GatewayError(Exception):
    """Raised when communication with the gateway fails."""
    pass


def submit_payment(
    amount,
    merchant,
    idempotency_key,
    currency="USD",
    agent_id="demo-agent",
    description=""
):
    """
    Submit a payment request to the safety gateway.

    The gateway, not the AI agent, makes the final
    financial safety decision.
    """

    # 1. Check authentication configuration
    if not AGENT_API_TOKEN:
        raise GatewayError(
            "AGENT_API_TOKEN is not configured."
        )

    # 2. Validate the idempotency key
    if not re.fullmatch(
        r"[A-Za-z0-9._:-]{8,64}",
        idempotency_key
    ):
        raise ValueError(
            "Invalid idempotency key."
        )

    # 3. Prepare the payment request
    payload = {
        "amount": str(amount),
        "merchant": merchant,
        "currency": currency,
        "agent_id": agent_id,
        "description": description
    }

    # 4. Prepare HTTP headers
    headers = {
        "Authorization": f"Bearer {AGENT_API_TOKEN}",
        "Idempotency-Key": idempotency_key
    }

    # 5. Send request to the gateway
    try:
        response = httpx.post(
            f"{GATEWAY_URL}/evaluate",
            json=payload,
            headers=headers,
            timeout=30.0
        )

        response.raise_for_status()

        return response.json()

    except httpx.HTTPStatusError as exc:
        try:
            detail = exc.response.json().get(
                "detail",
                exc.response.text
            )
        except ValueError:
            detail = exc.response.text

        raise GatewayError(
            f"Gateway returned HTTP "
            f"{exc.response.status_code}: {detail}"
        ) from exc

    except httpx.RequestError as exc:
        raise GatewayError(
            f"Could not communicate with gateway: {exc}"
        ) from exc
