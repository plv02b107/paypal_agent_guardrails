
from agent.llm import run_agent, confirm_payment


def display_payment_proposal(proposal):
    """Display the exact payment details awaiting confirmation."""

    arguments = proposal["arguments"]

    print("\n" + "=" * 50)
    print("PAYMENT CONFIRMATION REQUIRED")
    print("=" * 50)

    print(f"Amount      : {arguments.get('amount')}")
    print(f"Currency    : {arguments.get('currency')}")
    print(f"Merchant    : {arguments.get('merchant')}")
    print(f"Description : {arguments.get('description')}")

    print("=" * 50)
    print("Type CONFIRM to submit this payment.")
    print("Type CANCEL to discard this payment.")
    print("=" * 50)


def main():
    print("=" * 50)
    print("PAYMENT AI AGENT")
    print("Type 'exit' to quit.")
    print("=" * 50)

    pending_payment = None

    # Conversation memory for this terminal session.
    conversation_history = []

    while True:
        user_message = input("\nYou: ").strip()

        if user_message.lower() in ("exit", "quit"):
            print("Agent session ended.")
            break

        if not user_message:
            continue

        if pending_payment is not None:

            if user_message == "CONFIRM":
                try:
                    print("\nSubmitting confirmed payment...")

                    response = confirm_payment(
                        pending_payment,
                        conversation_history
                    )

                    print(f"\nAgent: {response}")

                except Exception as exc:
                    print(f"\nError: {exc}")

                finally:
                    pending_payment = None

                continue

            elif user_message == "CANCEL":
                pending_payment = None

                cancellation_message = (
                    "Payment request cancelled. No payment was submitted."
                )

                conversation_history.append({
                    "role": "assistant",
                    "content": cancellation_message
                })

                print(f"\nAgent: {cancellation_message}")
                continue

            else:
                print(
                    "\nAgent: A payment is awaiting confirmation. "
                    "Type CONFIRM to proceed or CANCEL to discard it."
                )
                continue

        try:
            response = run_agent(
                user_message,
                conversation_history
            )

            if (
                isinstance(response, dict)
                and response.get("type") == "payment_confirmation_required"
            ):
                pending_payment = response
                display_payment_proposal(pending_payment)

            else:
                print(f"\nAgent: {response}")

        except Exception as exc:
            print(f"\nError: {exc}")


if __name__ == "__main__":
    main()
