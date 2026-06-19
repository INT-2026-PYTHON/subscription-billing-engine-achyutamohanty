"""
BillingCycle — finds due subscriptions, generates invoices, posts ledger DEBITs,
advances the subscription period. Must be IDEMPOTENT (safe to run twice).
"""

def upgrade_subscription(self, subscription_id: int, new_plan_id: int, switch_date: date) -> Invoice:
        from billing_engine.billing.proration import compute_proration
        from billing_engine.models import Invoice, InvoiceStatus, InvoiceLineItem, LineItemKind, LedgerEntry, LedgerDirection
        from billing_engine.money import Money

        sub = self.subscription_repo.get(subscription_id)
        if not sub:
            raise ValueError(f"Subscription {subscription_id} not found.")

        old_plan = self.plan_repo.get(sub.plan_id)
        new_plan = self.plan_repo.get(new_plan_id)
        customer = self.customer_repo.get(sub.customer_id)

        old_strategy = self.strategy_factory(old_plan)
        new_strategy = self.strategy_factory(new_plan)

        old_price = old_strategy.calculate(0)
        new_price = new_strategy.calculate(0)

        tax_calc, tax_context = self.tax_factory(customer)

        pr = compute_proration(
            old_price, new_price, 
            sub.current_period_start, sub.current_period_end, 
            switch_date, tax_calc, tax_context
        )

        net_subtotal = pr.charge_amount - pr.credit_amount
        net_tax = pr.charge_tax - pr.credit_tax
        net_total = net_subtotal + net_tax

        with self.db.transaction():
            # Build Header
            invoice = Invoice(
                id=None, 
                subscription_id=sub.id,
                customer_id=sub.customer_id,
                period_start=switch_date, 
                period_end=sub.current_period_end,
                subtotal=net_subtotal,
                discount_total=Money(0, old_price.currency), 
                tax_total=net_tax,
                total_amount=net_total, 
                status=InvoiceStatus.DRAFT, 
                pdf_path=None,
                line_items=[]
            )
            saved = self.invoice_repo.add(invoice)

            # Build Line Items
            credit_item = InvoiceLineItem(
                id=None, invoice_id=saved.id,
                description=f"Prorated Credit for remaining days on {old_plan.name}",
                amount=-pr.credit_amount, kind=LineItemKind.PRORATION_CREDIT
            )
            self.line_item_repo.add(credit_item)

            charge_item = InvoiceLineItem(
                id=None, invoice_id=saved.id,
                description=f"Prorated Charge for remaining days on {new_plan.name}",
                amount=pr.charge_amount, kind=LineItemKind.PRORATION_CHARGE
            )
            self.line_item_repo.add(charge_item)

            # Ledger Entry
            self.ledger_repo.add(LedgerEntry(
                id=None, invoice_id=saved.id, customer_id=sub.customer_id,
                amount=net_total, currency=old_price.currency,
                direction=LedgerDirection.DEBIT, reason=f"Proration adjustments upgrade to {new_plan.name}"
            ))

            # Update core structural identity
            self.subscription_repo.update_plan(sub.id, new_plan_id)

        return saved