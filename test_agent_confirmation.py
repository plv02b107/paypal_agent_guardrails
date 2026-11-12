
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from agent import llm


def make_proposal():
    return {
        "type": "payment_confirmation_required",
        "arguments": {
            "amount": 100,
            "merchant": "Amazon",
            "currency": "USD",
            "description": "Shopping"
        },
        "tool_name": "submit_payment",
        "tool_call_id": "call_test_123",
        "conversation": [],
        "consumed": False
    }


def test_proposal_does_not_execute_payment():
    fake_call = SimpleNamespace(
        type="function_call",
        name="submit_payment",
        arguments='{"amount": 100, "merchant": "Amazon", '
                  '"currency": "USD", "description": "Shopping"}',
        call_id="call_test_123"
    )

    fake_response = SimpleNamespace(
        output=[fake_call],
        output_text=""
    )

    with patch.object(
        llm.client.responses,
        "create",
        return_value=fake_response
    ), patch.object(llm, "execute_tool") as mock_execute:

        result = llm.run_agent("Pay $100 to Amazon")

        assert result["type"] == "payment_confirmation_required"
        assert result["arguments"]["amount"] == 100
        mock_execute.assert_not_called()


def test_confirm_executes_payment():
    proposal = make_proposal()

    fake_response = SimpleNamespace(
        output_text="Payment request processed."
    )

    with patch.object(
        llm,
        "execute_tool",
        return_value={"status": "ALLOW"}
    ) as mock_execute, patch.object(
        llm.client.responses,
        "create",
        return_value=fake_response
    ):

        result = llm.confirm_payment(proposal)

        mock_execute.assert_called_once()

        assert result == "Payment request processed."
        assert proposal["consumed"] is True


def test_double_confirmation_is_rejected():
    proposal = make_proposal()
    proposal["consumed"] = True

    with patch.object(llm, "execute_tool") as mock_execute:

        with pytest.raises(ValueError):
            llm.confirm_payment(proposal)

        mock_execute.assert_not_called()


def test_invalid_proposal_is_rejected():

    with patch.object(llm, "execute_tool") as mock_execute:

        with pytest.raises(ValueError):
            llm.confirm_payment({})

        mock_execute.assert_not_called()



def test_cancel_and_unrecognized_input_never_execute_payment(monkeypatch):

    import agent.main as app

    proposal = make_proposal()

    inputs = iter([
        "Pay $100 to Amazon",
        "yes",
        "okay",
        "CANCEL",
        "exit"
    ])

    monkeypatch.setattr("builtins.input", lambda _: next(inputs))

    monkeypatch.setattr(
        app,
        "run_agent",
        lambda message, history=None: proposal    )

    with patch.object(app, "confirm_payment") as mock_confirm:

        app.main()

        mock_confirm.assert_not_called()

    assert proposal["consumed"] is False


def test_terminal_confirm_executes_exact_pending_proposal(monkeypatch):

    import agent.main as app

    proposal = make_proposal()

    inputs = iter([
        "Pay $100 to Amazon",
        "CONFIRM",
        "exit"
    ])

    monkeypatch.setattr("builtins.input", lambda _: next(inputs))

    monkeypatch.setattr(
        app,
        "run_agent",
        lambda message, history=None: proposal
    )

    with patch.object(
        app,
        "confirm_payment",
        return_value="Payment request processed."
    ) as mock_confirm:

        app.main()

        mock_confirm.assert_called_once_with(proposal, [])
