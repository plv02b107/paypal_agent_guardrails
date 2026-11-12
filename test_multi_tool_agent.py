
from types import SimpleNamespace
from unittest.mock import patch

from agent import llm


def make_tool_call(name, arguments, call_id):
    return SimpleNamespace(
        type="function_call",
        name=name,
        arguments=arguments,
        call_id=call_id
    )


def test_agent_executes_multiple_read_only_tools():

    budget_call = make_tool_call(
        "check_budget",
        "{}",
        "call_budget_123"
    )

    history_call = make_tool_call(
        "get_payment_history",
        '{"limit": 5, "status": null}',
        "call_history_456"
    )

    first_response = SimpleNamespace(
        output=[budget_call, history_call],
        output_text=""
    )

    final_response = SimpleNamespace(
        output=[],
        output_text="Your budget and payment history are ready."
    )

    with patch.object(
        llm.client.responses,
        "create",
        side_effect=[first_response, final_response]
    ) as mock_create, patch.object(
        llm,
        "execute_tool",
        side_effect=[
            {"available": "50.00"},
            {"transactions": []}
        ]
    ) as mock_execute:

        result = llm.run_agent(
            "Check my budget and recent payment history."
        )

    assert result == "Your budget and payment history are ready."

    assert mock_create.call_count == 2
    assert mock_execute.call_count == 2

    executed_names = [
        call.kwargs["name"]
        for call in mock_execute.call_args_list
    ]

    assert executed_names == [
        "check_budget",
        "get_payment_history"
    ]

    assert "submit_payment" not in executed_names


def test_read_only_tool_calls_do_not_execute_payments():

    budget_call = make_tool_call(
        "check_budget",
        "{}",
        "call_budget_789"
    )

    first_response = SimpleNamespace(
        output=[budget_call],
        output_text=""
    )

    final_response = SimpleNamespace(
        output=[],
        output_text="Your available budget is $50."
    )

    with patch.object(
        llm.client.responses,
        "create",
        side_effect=[first_response, final_response]
    ), patch.object(
        llm,
        "execute_tool",
        return_value={"available": "50.00"}
    ) as mock_execute:

        result = llm.run_agent("How much budget do I have?")

    assert result == "Your available budget is $50."

    mock_execute.assert_called_once()

    assert mock_execute.call_args.kwargs["name"] == "check_budget"
