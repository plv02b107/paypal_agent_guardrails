
from dataclasses import dataclass, field
from decimal import Decimal


@dataclass(frozen=True)
class Policy:
    # Hard restrictions. Human approval cannot override these.
    max_per_txn: Decimal = Decimal("50.00")
    daily_budget: Decimal = Decimal("100.00")

    # Automatic spending permissions by merchant.
    auto_limits: dict = field(default_factory=lambda: {
        "coffee-shop": Decimal("25.00"),
        "bookstore": Decimal("40.00"),
    })

    allowed_currencies: tuple = ("USD",)


POLICY = Policy()


def evaluate(
    amount: Decimal,
    currency: str,
    merchant: str,
    spent_and_reserved: Decimal,
):
    block = []
    review = []

    # Hard restrictions.
    if amount <= 0:
        block.append("Amount must be positive.")

    if currency not in POLICY.allowed_currencies:
        block.append(f"Currency {currency} is not allowed.")

    if amount > POLICY.max_per_txn:
        block.append(
            f"Amount exceeds the hard transaction limit "
            f"of {POLICY.max_per_txn}."
        )

    projected_total = spent_and_reserved + amount

    if projected_total > POLICY.daily_budget:
        block.append(
            f"Daily budget exceeded. Projected total: "
            f"{projected_total}."
        )

    # Automatic permission.
    auto_limit = POLICY.auto_limits.get(merchant)

    if auto_limit is None:
        review.append(
            f"Merchant {merchant} has no automatic permission."
        )
    elif amount > auto_limit:
        review.append(
            f"Amount exceeds the automatic limit of "
            f"{auto_limit} for {merchant}."
        )

    # Hard restrictions always take priority.
    if block:
        return "BLOCK", block + review

    if review:
        return "ASK_HUMAN", review

    return "ALLOW", ["Within all policy limits."]
