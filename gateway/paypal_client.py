
import os
import time
import threading
import requests

from dotenv import load_dotenv


# --------------------------------------------------
# 1. ENVIRONMENT CONFIGURATION
# --------------------------------------------------

load_dotenv()

BASE = os.getenv(
    "PAYPAL_BASE_URL",
    "https://api-m.sandbox.paypal.com"
).rstrip("/")

RETURN_URL = os.getenv(
    "PAYPAL_RETURN_URL",
    "http://127.0.0.1:8000/paypal/return"
)

CANCEL_URL = os.getenv(
    "PAYPAL_CANCEL_URL",
    "http://127.0.0.1:8000/paypal/cancel"
)


# --------------------------------------------------
# 2. ACCESS TOKEN CACHE
# --------------------------------------------------

_token = {
    "value": None,
    "expires_at": 0
}

_token_lock = threading.Lock()


# --------------------------------------------------
# 3. CUSTOM EXCEPTIONS
# --------------------------------------------------

class PayPalPreflightError(Exception):
    """Payment operation was not sent to PayPal."""


class PayPalDefinitiveError(Exception):
    """PayPal explicitly rejected the operation."""


class PayPalOutcomeUnknown(Exception):
    """PayPal may have processed the operation."""


# --------------------------------------------------
# 4. AUTHENTICATION
# --------------------------------------------------

def _headers(force_refresh=False):

    client_id = os.getenv("PAYPAL_CLIENT_ID")
    secret = os.getenv("PAYPAL_CLIENT_SECRET")

    if not client_id or not secret:
        raise PayPalPreflightError(
            "PayPal credentials are missing from .env."
        )

    with _token_lock:

        if force_refresh:
            _token["value"] = None
            _token["expires_at"] = 0

        if (
            _token["value"]
            and time.time() < _token["expires_at"]
        ):
            return {
                "Authorization": f"Bearer {_token['value']}",
                "Content-Type": "application/json"
            }

        try:
            response = requests.post(
                f"{BASE}/v1/oauth2/token",
                auth=(client_id, secret),
                data={
                    "grant_type": "client_credentials"
                },
                timeout=(5, 20)
            )

        except requests.RequestException as exc:
            raise PayPalPreflightError(
                "Could not reach PayPal token service. "
                "No payment operation was sent."
            ) from exc

        if response.status_code >= 400:
            raise PayPalPreflightError(
                f"PayPal token request failed with HTTP "
                f"{response.status_code}. "
                "No payment operation was sent."
            )

        try:
            data = response.json()

            access_token = data["access_token"]
            expires_in = int(data["expires_in"])

            if not access_token or expires_in <= 0:
                raise ValueError("Invalid token response.")

        except (ValueError, KeyError, TypeError) as exc:
            raise PayPalPreflightError(
                "PayPal returned an invalid access token response."
            ) from exc

        _token["value"] = access_token

        _token["expires_at"] = (
            time.time() + max(1, expires_in - 60)
        )

        return {
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json"
        }


# --------------------------------------------------
# 5. GENERIC PAYPAL API REQUEST
# --------------------------------------------------

def _request(
    method,
    path,
    request_id=None,
    json_body=None
):

    for attempt in range(2):

        headers = _headers(
            force_refresh=(attempt == 1)
        )

        if request_id:
            headers["PayPal-Request-Id"] = request_id

        try:
            response = requests.request(
                method,
                f"{BASE}{path}",
                headers=headers,
                json=json_body,
                timeout=(5, 30)
            )

        except requests.RequestException as exc:
            raise PayPalOutcomeUnknown(
                "Network error. PayPal may have received "
                "the request."
            ) from exc

        # Refresh authentication once after HTTP 401.
        if response.status_code == 401 and attempt == 0:
            continue

        if response.status_code == 401:
            raise PayPalPreflightError(
                "PayPal rejected the refreshed access token."
            )

        # Outcome may be uncertain.
        if (
            response.status_code >= 500
            or response.status_code in (409, 429)
        ):
            raise PayPalOutcomeUnknown(
                f"PayPal returned HTTP {response.status_code}. "
                "Reconciliation is required."
            )

        if response.status_code >= 400:
            raise PayPalDefinitiveError(
                f"PayPal rejected request: "
                f"{response.status_code} "
                f"{response.text[:500]}"
            )

        if not response.content:
            return {}

        try:
            return response.json()

        except ValueError as exc:
            raise PayPalOutcomeUnknown(
                "PayPal returned an invalid response. "
                "The operation may have succeeded."
            ) from exc

    raise PayPalPreflightError(
        "Could not authenticate with PayPal."
    )


# --------------------------------------------------
# 6. CREATE PAYPAL ORDER
# --------------------------------------------------

def create_order(
    amount,
    currency,
    description,
    request_id
):

    payload = {
        "intent": "CAPTURE",

        "purchase_units": [
            {
                "description": description[:120],

                "amount": {
                    "currency_code": currency,
                    "value": amount
                }
            }
        ],

        "application_context": {
            "return_url": RETURN_URL,
            "cancel_url": CANCEL_URL
        }
    }

    order = _request(
        "POST",
        "/v2/checkout/orders",
        request_id=request_id,
        json_body=payload
    )

    order_id = order.get("id")

    if not order_id:
        raise PayPalOutcomeUnknown(
            "PayPal response did not contain an order ID. "
            "The order may have been created."
        )

    approval_url = next(
        (
            link["href"]
            for link in order.get("links", [])
            if link.get("rel") in (
                "approve",
                "payer-action"
            )
        ),
        None
    )

    if not approval_url:
        raise PayPalOutcomeUnknown(
            "PayPal order was created but no approval URL "
            "was returned."
        )

    return {
        "id": order_id,
        "status": order.get("status", "UNKNOWN"),
        "approve_url": approval_url
    }


# --------------------------------------------------
# 7. CAPTURE PAYPAL ORDER
# --------------------------------------------------

def capture_order(
    order_id,
    request_id
):

    result = _request(
        "POST",
        f"/v2/checkout/orders/{order_id}/capture",
        request_id=request_id
    )

    return result


# --------------------------------------------------
# 8. GET PAYPAL ORDER DETAILS
# --------------------------------------------------

def get_order(order_id):

    return _request(
        "GET",
        f"/v2/checkout/orders/{order_id}"
    )
