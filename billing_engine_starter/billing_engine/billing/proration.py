"""
Proration — Day 4 stretch.

Mid-cycle plan change: customer is on Plan A from period_start to period_end,
but on `switch_date` they upgrade (or downgrade) to Plan B.

Day-count proration:
    total_days     = (period_end - period_start).days
    used_days      = (switch_date - period_start).days
    remaining_days = total_days - used_days

    credit = old_price * (remaining_days / total_days)
    charge = new_price * (remaining_days / total_days)

Tax MUST be recalculated on BOTH legs (reverse-tax on the credit,
fresh tax on the new charge). Tax is NOT prorated linearly — the tax
on a proration credit/charge is just `tax_calc.apply(credit_or_charge)`.

The two legs are returned as TAX-INCLUSIVE Money values for the
PRORATION_CREDIT (negative) and PRORATION_CHARGE (positive) line items.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from enum import Enum
from typing import NamedTuple, Optional

from billing_engine.models import LedgerEntry, LedgerDirection, SubscriptionStatus, Invoice

class DunningState(Enum):
    SUCCEEDED = "SUCCEEDED"
    RETRYING = "RETRYING"
    FAILED_FINAL = "FAILED_FINAL"

class DunningOutcome(NamedTuple):
    state: DunningState
    attempt_no: int
    next_retry_at: Optional[datetime]

MAX_ATTEMPTS = 3
RETRY_DELAYS_DAYS = {1: 1, 2: 3, 3: 7}  # Map attempt_no to wait durations

class DunningProcess:
    def __init__(self, gateway, invoice_repo, attempt_repo, subscription_repo, ledger_repo):
        self.gateway = gateway
        self.invoice_repo = invoice_repo
        self.attempt_repo = attempt_repo
        self.subscription_repo = subscription_repo
        self.ledger_repo = ledger_repo

    def attempt(self, invoice: Invoice, customer_id: int, now: datetime) -> DunningOutcome:
        attempt_no = self.attempt_repo.count_for_invoice(invoice.id) + 1
        result = self.gateway.charge(invoice)

        if result.success:
            self.invoice_repo.mark_paid(invoice.id)
            self.ledger_repo.add(LedgerEntry(
                id=None, invoice_id=invoice.id, customer_id=customer_id,
                amount=invoice.total_amount, currency=invoice.subtotal.currency,
                direction=LedgerDirection.CREDIT,
                reason=f"Payment received for invoice {invoice.id}",
            ))
            self.attempt_repo.add(invoice.id, attempt_no, "SUCCESS", None, None)
            return DunningOutcome(DunningState.SUCCEEDED, attempt_no, None)

        if attempt_no >= MAX_ATTEMPTS:
            self.invoice_repo.mark_failed(invoice.id)
            self.subscription_repo.update_status(
                invoice.subscription_id, SubscriptionStatus.PAST_DUE,
                past_due_since=now.date(),
            )
            self.attempt_repo.add(invoice.id, attempt_no, "FAILED", result.failure_reason, None)
            return DunningOutcome(DunningState.FAILED_FINAL, attempt_no, None)

        delay = RETRY_DELAYS_DAYS.get(attempt_no, 1)
        next_retry = now + timedelta(days=delay)
        self.attempt_repo.add(invoice.id, attempt_no, "FAILED", result.failure_reason, next_retry)
        return DunningOutcome(DunningState.RETRYING, attempt_no, next_retry)

    @staticmethod
    def should_cancel(past_due_since: Optional[date], today: date, grace_days: int = 7) -> bool:
        if past_due_since is None:
            return False
        return (today - past_due_since).days >= grace_daysx