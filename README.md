# PayPal Agent Guardrails

An AI-powered payment assistant with a dedicated safety gateway, policy-based transaction evaluation, human approval, and PayPal Sandbox integration.

## 1. Overview

PayPal Agent Guardrails is a project that explores how an AI agent can assist with payment operations without having unrestricted authority to execute financial transactions.

A user can interact with the agent conversationally, for example:

> I want to buy a $10 coffee from Halsey Coffee Shop.

The agent extracts the payment details and presents a proposal. It does not directly communicate with PayPal or execute the payment.

Instead, the request passes through a separate FastAPI gateway. The gateway validates the request, checks transaction policies, manages reservations, and communicates with the PayPal Sandbox API.

Depending on the policy decision, a transaction can be automatically processed, sent for human approval, or blocked.

The project focuses on a central design principle:

**An AI agent may propose a financial action, but the authority to execute that action must remain controlled by deterministic application logic.**

## 2. Problem Statement

AI agents are increasingly capable of interacting with external tools and services. However, allowing an AI agent to execute financial transactions directly introduces several risks:

- Incorrect amounts or merchant details.
- Unauthorized or unintended payments.
- Repeated transactions caused by retries.
- Transactions exceeding predefined spending limits.
- Uncertain outcomes when an external payment API becomes unavailable.
- Lack of traceability when investigating payment operations.

A conversational confirmation alone is not sufficient protection for a financial workflow.

This project addresses these problems by separating the AI agent from the payment execution layer and introducing independent validation, policy enforcement, authentication, transaction tracking, and audit logging.

## 3. Project Objectives

The main objectives are:

1. Build a conversational AI agent that can understand payment requests.
2. Require explicit confirmation before submitting a payment request.
3. Separate AI decision-making from actual payment execution.
4. Implement a FastAPI gateway as the controlled entry point for payment operations.
5. Enforce transaction limits and merchant-specific policies.
6. Support human approval for transactions requiring additional review.
7. Integrate with PayPal Sandbox for order creation and capture.
8. Prevent duplicate operations through idempotency mechanisms.
9. Track transaction states and maintain an audit trail.
10. Handle uncertain API outcomes through reconciliation rather than blindly repeating payment operations.

## 4. System Architecture

The application consists of three main layers:

### Layer 1: AI Agent

The agent handles natural-language interaction.

It:
- Understands the user's payment request.
- Extracts amount, currency, merchant, and description.
- Uses read-only tools to retrieve information when required.
- Creates a payment proposal.
- Waits for explicit application-level confirmation.

The agent does not receive the human approval credentials and does not communicate directly with PayPal.

### Layer 2: Payment Safety Gateway

The gateway is implemented using FastAPI.

It:
- Authenticates incoming agent requests.
- Validates transaction details.
- Checks idempotency keys.
- Evaluates the transaction against predefined policies.
- Manages human approval.
- Creates PayPal orders.
- Handles capture requests.
- Maintains transaction states and audit events.
- Supports retry and reconciliation workflows.

### Layer 3: PayPal Sandbox

PayPal Sandbox simulates the external payment service.

It is responsible for:
- Creating payment orders.
- Providing buyer approval links.
- Returning order details.
- Processing capture requests.
- Returning transaction statuses.

### High-Level Architecture

```text
                  USER
                    |
                    v
             AI PAYMENT AGENT
                    |
          Understands payment request
                    |
          Creates payment proposal
                    |
          Explicit CONFIRM required
                    |
                    v
             AGENT TOOLS
                    |
                    v
          FASTAPI SAFETY GATEWAY
                    |
          Authentication and validation
                    |
             Idempotency check
                    |
              Policy Engine
                    |
          +---------+----------+
          |         |          |
          v         v          v
        ALLOW    ASK_HUMAN    BLOCK
          |         |          |
          |    Human decision  |
          |         |          |
          +---------+          |
                    |          |
                    v          v
              PAYPAL ORDER
                    |
             Buyer approval
                    |
                    v
             PAYPAL SANDBOX
                    |
              Capture payment
                    |
                    v
           Transaction database
                    |
                    v
              Audit logging
```

## 5. Technology Stack

| Component | Technology |
|---|---|
| Programming language | Python |
| AI model | Groq-hosted GPT-OSS 120B |
| AI client | OpenAI-compatible API client |
| Backend framework | FastAPI |
| API server | Uvicorn |
| HTTP communication | HTTPX and Requests |
| Data validation | Pydantic |
| Database | SQLite |
| Payment provider | PayPal Sandbox |
| Environment management | python-dotenv |
| Testing | Pytest |
| Development environment | Python virtual environment |

## 6. AI Agent Implementation

The AI agent is implemented primarily in `agent/llm.py`.

It uses the Groq API through an OpenAI-compatible client.

The agent receives a user message and maintains conversation history. It can invoke predefined tools to perform supported operations.

Examples include:

- `submit_payment`
- `check_budget`
- `get_payment_history`
- `check_transaction_status`

The tools have different responsibilities.

Read-only tools can execute during the agent's conversational tool loop. Payment submission is treated differently.

When the model requests `submit_payment`, the application:
1. Parses and validates the proposed arguments.
2. Ensures that a payment proposal is not mixed with other tool calls.
3. Stores the tool arguments and tool-call ID in a pending proposal.
4. Returns the proposal to the CLI without executing the payment.

This separation prevents the model from directly triggering a payment merely because it generated a tool call.

## 7. Three Separate Approval Stages

An important part of the architecture is that it uses three distinct approval stages.

They serve different purposes and should not be confused.

### Stage 1: Application-Level Confirmation

The CLI displays the proposed payment details:

- Amount
- Currency
- Merchant
- Description

The user must type the exact word `CONFIRM` to proceed.

Typing `CANCEL` discards the proposal. Other messages do not authorize the payment while a proposal is pending.

The application also marks a proposal as consumed before attempting execution, preventing the same proposal from being reused.

### Stage 2: Gateway-Level Human Approval

After the agent submits a request to the gateway, the policy engine may return `ASK_HUMAN`.

The transaction then enters `pending_approval`.

A separate authenticated human approval endpoint allows an authorized reviewer to approve or reject the transaction.

The gateway rechecks hard policy constraints during approval. Human approval cannot override hard transaction limits or the daily budget.

### Stage 3: PayPal Buyer Approval

After a PayPal order is created, the buyer receives an approval URL.

The buyer must approve the order through PayPal.

When PayPal redirects the buyer to the return endpoint, the gateway fetches the order details and checks that the order status is `APPROVED` before initiating capture.

This is separate from both the CLI confirmation and gateway-level human approval.

## 8. Payment Safety Gateway

The gateway is implemented in `gateway/main.py`.

It acts as the only controlled route between the agent and PayPal.

Important responsibilities include:

- Authentication using bearer tokens.
- Request validation.
- Idempotency handling.
- Policy evaluation.
- Transaction creation and state management.
- Human approval.
- PayPal order creation and capture.
- Reconciliation and recovery.
- Audit logging.

### Important API Endpoints

| Endpoint | Method | Purpose |
|---|---|---|
| `/health` | GET | Gateway health check |
| `/evaluate` | POST | Validate and evaluate a payment request |
| `/approvals` | GET | List transactions awaiting human approval |
| `/approvals/{tx_id}/decision` | POST | Approve or reject a transaction |
| `/transactions` | GET | List transactions |
| `/transactions/{tx_id}/reconcile` | POST | Reconcile transaction status with PayPal |
| `/transactions/{tx_id}/recover-stale` | POST | Recover stale operations into an uncertain state |
| `/transactions/{tx_id}/retry-create` | POST | Retry an eligible PayPal order creation |
| `/orders/{order_id}/capture` | POST | Capture an approved order |
| `/paypal/return` | GET | Handle the PayPal buyer return |
| `/paypal/cancel` | GET | Handle the PayPal buyer cancellation |
| `/audit` | GET | Retrieve audit events |
| `/policy` | GET | View policy configuration |

Agent-facing and human-facing operations use separate authentication credentials.

The agent token is used for agent operations. The human approval token is used for approval and administrative operations.

## 9. Policy Engine

The policy engine is implemented in `gateway/policy.py`.

It evaluates transactions using predefined rules.

### Current Policy Configuration

| Rule | Limit |
|---|---:|
| Maximum transaction amount | $50 |
| Daily spending budget | $100 |
| Coffee shop automatic approval limit | $25 |
| Bookstore automatic approval limit | $40 |
| Allowed currency | USD |

These are configurable project-level rules, not PayPal-imposed limits.

### Policy Decisions

The engine returns one of three decisions.

**ALLOW**

The transaction satisfies the automatic approval conditions.

The gateway can proceed with PayPal order creation.

**ASK_HUMAN**

The transaction requires additional review, such as when the merchant is not in the automatic approval list or the amount exceeds the merchant-specific automatic limit.

The transaction enters `pending_approval`.

**BLOCK**

The transaction violates a hard policy rule.

Examples include:
- Non-positive transaction amounts.
- Unsupported currencies.
- Amount exceeding the maximum transaction limit.
- Projected spending exceeding the daily budget.

A blocked transaction cannot proceed to PayPal order creation.

Hard policy restrictions take precedence over human approval.

## 10. Transaction Database and State Management

The project uses SQLite, implemented in `gateway/db.py`.

The database stores transaction details, idempotency information, reservation amounts, timestamps, and audit events.

Instead of treating a payment as a single API call, the application tracks its progress through explicit states.

### Main Transaction States

| State | Meaning |
|---|---|
| `pending_approval` | Waiting for a human decision |
| `creating_order` | PayPal order creation is in progress |
| `create_retryable` | Order creation request was not sent and can be retried |
| `create_unknown` | PayPal may have created an order, but the result is uncertain |
| `awaiting_buyer` | PayPal order exists and awaits buyer action |
| `capturing` | Capture operation is in progress |
| `capture_unknown` | Capture may have succeeded, but the result is uncertain |
| `capture_pending` | Capture has not reached a final state |
| `captured` | Payment capture completed |
| `failed` | Payment operation failed |
| `blocked` | Transaction rejected by policy |
| `rejected` | Human reviewer rejected the transaction |
| `expired` | Pending approval expired |

The database uses explicit transition rules to prevent invalid state changes.

Critical database operations use SQLite transactions and `BEGIN IMMEDIATE` to reduce the risk of concurrent operations interfering with each other.

### Transaction Reservations

The gateway accounts for both completed spending and reserved amounts.

A transaction awaiting human approval reserves its amount so that multiple pending transactions cannot independently consume the same remaining daily budget.

Reservations are released when the transaction reaches an appropriate terminal state, such as captured, failed, blocked, or rejected.

Uncertain payment outcomes retain their reservations because the gateway must not assume that the external payment operation failed.

## 11. Idempotency and Duplicate Prevention

Payment APIs must handle retries carefully.

For example, suppose the gateway sends a request to PayPal, but the network connection times out before the response reaches the application.

The gateway cannot safely assume that PayPal did not create the order.

Blindly sending another request could create duplicate operations.

This project implements idempotency at multiple levels.

### Gateway-Level Idempotency

The agent sends an `Idempotency-Key` header with each payment request.

The gateway stores the key alongside a request hash.

- Same key and same request details: return the existing transaction.
- Same key and different request details: reject the request with HTTP 409.

This prevents a repeated request from creating a second gateway transaction.

### PayPal Order Creation Idempotency

The gateway uses the transaction ID as the PayPal request ID:

```python
request_id=tx_id
```

The PayPal client sends this through the `PayPal-Request-Id` header.

The same gateway transaction therefore uses a stable identifier for its order-creation request.

### PayPal Capture Idempotency

Capture uses a deterministic request ID generated from the transaction ID:

```python
uuid.uuid5(
    uuid.NAMESPACE_URL,
    f"paypal-capture:{transaction_id}"
)
```

This ensures that retries for the same transaction use the same capture request identifier.

## 12. Retry Handling and Reconciliation

A key design decision is that an uncertain payment outcome must not automatically be treated as a failure.

The gateway distinguishes between three broad situations.

### Preflight Failure

The request was not sent to PayPal.

The gateway can safely mark the operation as retryable.

### Definitive Failure

PayPal returned a response indicating that the operation was rejected.

The transaction can move to a failed state and its reservation can be released.

### Unknown Outcome

The request may have reached PayPal, but the gateway cannot establish the final outcome.

The transaction is moved into an uncertain state and its reservation is retained.

### Reconciliation

The human-authenticated reconciliation endpoint fetches the PayPal order and compares its status with the local transaction record.

Depending on the returned PayPal status:

- `COMPLETED`: mark the transaction as captured.
- `DECLINED` or `VOIDED`: mark it as failed.
- `CREATED`, `APPROVED`, or `PAYER_ACTION_REQUIRED`: return it to `awaiting_buyer`.
- Other statuses: retain the uncertain state and reservation.

Reconciliation checks the external status. It does not independently initiate a new capture.

### Stale Operation Recovery

If an order creation or capture operation remains in progress for more than ten minutes, the recovery endpoint can move it into an uncertain state.

It does not blindly repeat the PayPal operation.

The goal is to establish the external payment outcome before deciding whether another operation is safe.

## 13. PayPal Sandbox Integration

The PayPal integration is implemented in `gateway/paypal_client.py`.

It communicates with the PayPal Sandbox API.

The main operations are:

1. Obtain an OAuth access token.
2. Create a PayPal order.
3. Return the buyer approval URL.
4. Retrieve order details.
5. Capture an approved order.

The client includes:
- Access-token caching.
- Thread-safe token refresh.
- HTTP timeout handling.
- Retry handling for an authentication failure.
- Classification of preflight, definitive, and uncertain outcomes.

The gateway uses the buyer return endpoint to verify that the PayPal order is approved before capture.

The buyer cancellation endpoint does not initiate capture. It also does not immediately change the transaction state or release its reservation. The transaction remains available for the existing recovery or expiry workflow.

For local development, the default callback URLs use `127.0.0.1`. A remotely hosted PayPal checkout generally needs a callback URL reachable by the buyer's browser. A public HTTPS tunnel, such as ngrok, can be used during local testing.

## 14. Audit Logging

The gateway records important transaction events in the database.

Examples include:

- Transaction creation.
- Human approval or rejection.
- Policy-based blocking.
- PayPal order creation.
- Capture initiation.
- Capture completion.
- Reconciliation.
- Recovery from stale operations.

Audit events help reconstruct what happened during a transaction and provide visibility into state changes.

The project also exposes an authenticated audit endpoint for retrieving these records.

## 15. Project Structure

```text
paypal_agent_guardrails/
│
├── agent/
│   ├── main.py
│   ├── llm.py
│   ├── tools.py
│   └── gateway_client.py
│
├── gateway/
│   ├── main.py
│   ├── db.py
│   ├── policy.py
│   └── paypal_client.py
│
├── test_agent_confirmation.py
├── test_agent_end_to_end.py
├── test_capture.py
├── test_multi_tool_agent.py
├── test_reconcile.py
├── test_retry.py
│
├── manual_paypal_sandbox.py
├── quickstart.py
├── requirements.txt
├── .env.example
├── .gitignore
└── README.md
```

The test files are currently located in the project root.

`manual_paypal_sandbox.py` is a separate manual PayPal Sandbox experiment. It is not the normal guarded payment flow.

`quickstart.py` is a separate experiment using additional agent-related packages. It is not required for the main application flow.

## 16. Installation and Setup

### Step 1: Clone the Repository

```bash
git clone <repository-url>
cd paypal_agent_guardrails
```

### Step 2: Create a Virtual Environment

```bash
python -m venv venv
```

Activate it in Windows PowerShell:

```powershell
.\venv\Scripts\Activate.ps1
```

### Step 3: Install Dependencies

```bash
pip install -r requirements.txt
```

### Step 4: Configure Environment Variables

Create a local `.env` file using the example:

```powershell
Copy-Item .env.example .env
```

Add your own credentials and configuration:

```dotenv
GROQ_API_KEY=your_groq_api_key

AGENT_API_TOKEN=your_agent_token
HUMAN_APPROVAL_TOKEN=your_human_approval_token

GATEWAY_BASE_URL=http://127.0.0.1:8000

GUARDRAIL_DB_PATH=guardrail_v2.db

PAYPAL_CLIENT_ID=your_paypal_sandbox_client_id
PAYPAL_CLIENT_SECRET=your_paypal_sandbox_secret

PAYPAL_BASE_URL=https://api-m.sandbox.paypal.com

PAYPAL_RETURN_URL=http://127.0.0.1:8000/paypal/return
PAYPAL_CANCEL_URL=http://127.0.0.1:8000/paypal/cancel
```

Do not commit the actual `.env` file or expose API credentials.

The `.env.example` file contains placeholders and can be committed.

### Step 5: Start the Gateway

Open the first terminal:

```powershell
uvicorn gateway.main:app --host 127.0.0.1 --port 8000
```

The gateway should be available at:

```text
http://127.0.0.1:8000
```

### Step 6: Start the AI Agent

Open a second terminal, activate the same virtual environment, and run:

```powershell
python -m agent.main
```

The agent can now accept conversational payment requests.

## 17. Running Tests

The project uses Pytest.

Run the complete test suite:

```powershell
python -m pytest -q
```

**Latest verified result:**

```text
36 passed
```

The tests cover areas including:
- Explicit agent confirmation.
- Agent and gateway integration.
- Multiple tool interactions.
- Payment capture behavior.
- Retry handling.
- Transaction reconciliation.

The test suite uses test-specific configuration and should not be confused with a live PayPal transaction.

## 18. Security Considerations

The project uses multiple independent safeguards:

- The agent does not directly communicate with PayPal.
- Payment proposals require explicit user confirmation.
- Agent and human approval credentials are separate.
- The gateway validates transaction inputs.
- Policy decisions are enforced by deterministic code.
- Human approval cannot override hard policy restrictions.
- Idempotency keys reduce duplicate transaction risk.
- Uncertain outcomes are reconciled instead of blindly retried.
- Transaction state changes are recorded in an audit trail.
- API credentials are stored in environment variables rather than source code.

These safeguards reduce risk, but they do not make the current implementation suitable for handling real customer funds.

## 19. Current Limitations and Future Improvements

This is a development and learning project using local services, SQLite, and PayPal Sandbox.

Potential improvements include:

1. Move from SQLite to PostgreSQL for a more robust multi-user deployment.
2. Add role-based access control for approval and administrative operations.
3. Improve reservation expiry and cleanup for transactions awaiting buyer action.
4. Add stronger monitoring and alerting for uncertain transactions.
5. Add structured logs and request correlation IDs.
6. Introduce integration tests against a controlled PayPal Sandbox environment.
7. Add deployment configuration and HTTPS support.
8. Add rate limiting and additional API security controls.
9. Improve recovery workflows for transactions with no known PayPal order ID.
10. Add a dedicated dashboard for transaction monitoring and human approvals.

The current implementation is not production-ready and should only be used with test credentials and the PayPal Sandbox.

## 20. Key Learning Outcomes

This project provided practical experience with:

- Building tool-calling AI agents.
- Connecting an LLM to external application tools.
- Separating probabilistic model behavior from deterministic business logic.
- Designing REST APIs using FastAPI.
- Implementing bearer-token authentication.
- Using Pydantic for request validation.
- Designing transaction state machines.
- Working with SQLite transactions and audit records.
- Integrating an external payment API.
- Handling idempotency and uncertain API outcomes.
- Implementing human-in-the-loop workflows.
- Writing automated tests for payment-related application logic.

## 21. Conclusion

PayPal Agent Guardrails demonstrates how an AI agent can participate in a sensitive workflow without being given unrestricted execution authority.

The agent handles conversation and payment proposals, while the gateway remains responsible for validation, policy enforcement, approval, transaction management, and communication with PayPal.

By combining explicit confirmation, independent human approval, deterministic policy checks, idempotency, transaction state management, and reconciliation, the project explores a safer architecture for AI-assisted financial operations.

**The central idea is simple: the AI can propose a payment, but the application controls whether that payment is allowed to proceed.**