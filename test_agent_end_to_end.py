
from unittest.mock import patch

from agent import main as app


def test_budget_to_payment_confirmation_flow(monkeypatch):

    proposal = {
        "type": "payment_confirmation_required",
        "arguments": {
            "amount": 100,
            "merchant": "Amazon",
            "currency": "USD",
            "description": "Shopping"
        },
        "tool_name": "submit_payment",
        "tool_call_id": "call_e2e_123",
        "conversation": [],
        "consumed": False
    }

    inputs = iter([
        "How much budget do I have?",
        "Pay $100 to Amazon",
        "CONFIRM",
        "exit"
    ])

    monkeypatch.setattr(
        "builtins.input",
        lambda _: next(inputs)
    )

    with patch.object(
        app,
        "run_agent",
        side_effect=[
            "Your available budget is $50.",
            proposal
        ]
    ) as mock_run_agent, patch.object(
        app,
        "confirm_payment",
        return_value="Payment request processed."
    ) as mock_confirm:

        app.main()

        assert mock_run_agent.call_count == 2

        mock_confirm.assert_called_once_with(
            proposal,
            mock_run_agent.call_args_list[0].args[1]
        )

    assert proposal["consumed"] is False
