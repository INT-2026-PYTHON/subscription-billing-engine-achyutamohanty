"""
build_invoice — PURE function that turns inputs into an Invoice dataclass.

⚠️ NO database calls here. No `datetime.now()`. No PDF. Just math.

The order is FIXED:
    1. base       = strategy.calculate(usage)
    2. discount   = discount.apply(base) if discount else 0
    3. taxable    = base - discount
    4. tax        = tax_calc.apply(taxable)
    5. total      = taxable + tax.total
"""
from datetime import date
from typing import Optional
from billing_engine.models import (
    Invoice,
    InvoiceStatus,
    InvoiceLineItem,
    LineItemKind,
    Plan,
    Customer,
    Subscription,
)
from billing_engine.money import Money
from billing_engine.discounts.base import DiscountContext

def build_invoice(
    subscription: Subscription,
    customer: Customer,
    plan: Plan,
    period_start: date,
    period_end: date,
    usage_quantity: int,
    invoice_count_so_far: int,
    strategy,          # Day 1 PricingStrategy
    discount,          # Day 1 Discount Strategy (Optional)
    tax_calc,          # Day 1 Tax Calculator
    tax_context,       # Instantiated Tax Context
) -> Invoice:
    """Builds an immutable Invoice instance with structured line items.

    This function operates purely on domain objects, generating the complete breakdown
    of charges, discounts, and taxes natively before saving.
    """
    # 1. Compute base gross charge
    base_amount = strategy.calculate(usage_quantity)
    currency = base_amount.currency

    # 2. Compute discount line item
    if discount is None:
        discount_amount = Money(0, currency)
    else:
        discount_context = DiscountContext(
            invoice_count_so_far=invoice_count_so_far
        )
        discount_amount = discount.apply(base_amount, discount_context)

    # 3. Handle base taxable ceiling
    taxable_amount = base_amount - discount_amount

    # 4. Generate tax breakdown matrices
    tax_result = tax_calc.apply(taxable_amount, tax_context)
    
    # 5. Total summation
    total_amount = taxable_amount + tax_result.total

    # 6. Structuring line items sequence
    line_items = [
        InvoiceLineItem(
            id=None,
            invoice_id=None,
            kind=LineItemKind.BASE,
            amount=base_amount,
            description=f"{plan.name} Base Service Charge ({period_start} to {period_end})",
        )
    ]

    # Append discount tracking only if it actively altered the subtotal
    if discount_amount > Money(0, currency):
        line_items.append(
            InvoiceLineItem(
                id=None,
                invoice_id=None,
                kind=LineItemKind.DISCOUNT,
                amount=-discount_amount,  # Persisted as negative per criteria
                description=f"Discount applied: {discount.__class__.__name__ if hasattr(discount, '__class__') else 'Promo'}"
            )
        )

    # Deconstruct and append tax calculations dynamically
    for label, amt in tax_result.components:
        line_items.append(
            InvoiceLineItem(
                id=None,
                invoice_id=None,
                kind=LineItemKind.TAX,
                amount=amt,
                description=label,
            )
        )

    # 7. Unify everything inside a transient draft invoice
    return Invoice(
        id=None,
        subscription_id=subscription.id,
        customer_id=subscription.customer_id,
        status=InvoiceStatus.DRAFT,
        period_start=period_start,
        period_end=period_end,
        subtotal=base_amount,
        discount_total=discount_amount,
        tax_total=tax_result.total,
        total_amount=total_amount,
        line_items=line_items
    )