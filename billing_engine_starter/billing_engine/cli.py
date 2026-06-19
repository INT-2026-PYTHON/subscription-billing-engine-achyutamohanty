"""
CLI entrypoint.

Subcommands to implement (Day 4):
    billing init                              -- create / migrate the DB
    billing customer add <name> <email> <country> [--state CODE]
    billing plan list
    billing subscribe <customer_id> <plan_id> [--trial-days N] [--discount CODE]
    billing bill run [--date YYYY-MM-DD]
    billing invoice show <invoice_id>          -- prints PLAIN TEXT invoice
    billing upgrade <subscription_id> <new_plan_id> [--date YYYY-MM-DD]   (STRETCH)
    billing demo                              -- run the scripted scenario

Use argparse with subparsers. Keep each subcommand handler in its own function.

PDF rendering is OUT OF SCOPE for the core project — `invoice show` should
print a clean PLAIN-TEXT invoice (see helper `format_invoice_text` below).
PDF generation is BONUS: see `billing_engine/pdf/renderer.py`.
"""

from __future__ import annotations

import argparse
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

from billing_engine.db import (
    Database,
    CustomerRepository, PlanRepository, SubscriptionRepository,
    UsageRecordRepository, InvoiceRepository, InvoiceLineItemRepository,
    LedgerRepository, DiscountRepository, PlanTierRepository
)
from billing_engine.models import (
    Invoice, Customer, Plan, Subscription, SubscriptionStatus, 
    PricingType, BillingPeriod, Money
)
from billing_engine.cycle import BillingCycle
from billing_engine.pricing.flat_rate import FlatRate
from billing_engine.taxes.gst import GSTCalculator
from billing_engine.taxes.base import TaxContext
from decimal import Decimal

DB_PATH = "billing.db"


def format_invoice_text(invoice: Invoice, customer_name: str, plan_name: str) -> str:
    lines = []
    lines.append(f"INVOICE #{invoice.id or 'DRAFT'}")
    lines.append("============================================================")
    lines.append(f"Customer: {customer_name}")
    lines.append(f"Plan:     {plan_name}")
    lines.append(f"Period:   {invoice.period_start} to {invoice.period_end}")
    lines.append("------------------------------------------------------------")
    
    currency_symbol = "₹" if invoice.subtotal.currency == "INR" else f"{invoice.subtotal.currency} "
    
    for item in invoice.line_items:
        desc = item.description
        val_str = f"{currency_symbol}{float(item.amount.to_storage()):.2f}"
        lines.append(f"{desc:<45}{val_str:>15}")
        
    lines.append("------------------------------------------------------------")
    total_val = f"{currency_symbol}{float(invoice.total_amount.to_storage()):.2f}"
    lines.append(f"TOTAL{r'':<40}{total_val:>15}")
    status_str = invoice.status.value if hasattr(invoice.status, "value") else str(invoice.status)
    lines.append(f"Status: {status_str.upper()}")
    
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="billing", description="Subscription Billing CLI")
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("init", help="initialize the database")
    
    cust_parser = sub.add_parser("customer", help="manage customers")
    cust_sub = cust_parser.add_subparsers(dest="subcmd", required=True)
    cust_add = cust_sub.add_parser("add", help="add a customer")
    cust_add.add_argument("name")
    cust_add.add_argument("email")
    cust_add.add_argument("country")
    cust_add.add_argument("--state", default="")

    plan_parser = sub.add_parser("plan", help="manage plans")
    plan_sub = plan_parser.add_subparsers(dest="subcmd", required=True)
    plan_sub.add_parser("list", help="list all plans")

    sub_parser = sub.add_parser("subscribe", help="subscribe a customer to a plan")
    sub_parser.add_argument("customer_id", type=int)
    sub_parser.add_argument("plan_id", type=int)
    sub_parser.add_argument("--trial-days", type=int, default=0)
    sub_parser.add_argument("--discount", default=None)

    bill_parser = sub.add_parser("bill", help="run engine billing cycle")
    bill_sub = bill_parser.add_subparsers(dest="subcmd", required=True)
    bill_run = bill_sub.add_parser("run", help="execute billing process")
    bill_run.add_argument("--date", default=None)

    inv_parser = sub.add_parser("invoice", help="manage invoices")
    inv_sub = inv_parser.add_subparsers(dest="subcmd", required=True)
    inv_show = inv_sub.add_parser("show", help="display details of an invoice")
    inv_show.add_argument("invoice_id", type=int)

    upgrade_parser = sub.add_parser("upgrade", help="upgrade a subscription plan mid-cycle")
    upgrade_parser.add_argument("subscription_id", type=int)
    upgrade_parser.add_argument("new_plan_id", type=int)
    upgrade_parser.add_argument("--date", default=None)

    sub.add_parser("demo", help="run the demo scenario")

    args = parser.parse_args(argv)
    db = Database(DB_PATH)

    if args.cmd == "init":
        db.init_schema()
        print("Database initialized successfully.")
        return 0

    if args.cmd == "customer" and args.subcmd == "add":
        repo = CustomerRepository(db)
        customer = Customer(id=None, name=args.name, email=args.email, currency="INR", tax_country=args.country, tax_state=args.state)
        repo.add(customer)
        print(f"Customer created successfully with ID: {customer.id}")
        return 0

    if args.cmd == "plan" and args.subcmd == "list":
        repo = PlanRepository(db)
        plans = repo.list_all()
        for p in plans:
            print(f"[{p.id}] {p.name} - {p.pricing_type} ({p.billing_period})")
        return 0

    if args.cmd == "subscribe":
        repo = SubscriptionRepository(db)
        start_date = date.today()
        end_date = start_date + timedelta(days=30)
        trial_end = start_date + timedelta(days=args.trial_days) if args.trial_days > 0 else None
        status = SubscriptionStatus.TRIALING if trial_end else SubscriptionStatus.ACTIVE
        
        disc_repo = DiscountRepository(db)
        discount_id = None
        if args.discount:
            disc_row = disc_repo.get_by_code(args.discount)
            if disc_row:
                discount_id = disc_row["id"]

        sub_model = Subscription(
            id=None, customer_id=args.customer_id, plan_id=args.plan_id, status=status,
            current_period_start=start_date, current_period_end=end_date, trial_end=trial_end,
            past_due_since=None
        )
        sub_model.discount_id = discount_id
        repo.add(sub_model)
        print(f"Subscription created successfully with ID: {sub_model.id}")
        return 0

    if args.cmd == "bill" and args.subcmd == "run":
        as_of = date.fromisoformat(args.date) if args.date else date.today()
        
        def mock_factory(x): return FlatRate(Money(1000, "INR"))
        def mock_discount(x): return None
        def mock_tax(c): return (GSTCalculator(Decimal("0.09"), Decimal("0.09"), Decimal("0.18")), TaxContext(customer_state=c.tax_state, seller_state="KA"))

        engine = BillingCycle(
            db, CustomerRepository(db), PlanRepository(db), SubscriptionRepository(db),
            UsageRecordRepository(db), InvoiceRepository(db), InvoiceLineItemRepository(db),
            LedgerRepository(db), mock_factory, mock_discount, mock_tax
        )
        result = engine.run(as_of)
        print(f"Billing completed. Created: {result.invoices_created}, Skipped: {result.invoices_skipped_duplicate}, Trials Activated: {result.trials_activated}")
        return 0

    if args.cmd == "invoice" and args.subcmd == "show":
        inv_repo = InvoiceRepository(db)
        line_repo = InvoiceLineItemRepository(db)
        cust_repo = CustomerRepository(db)
        sub_repo = SubscriptionRepository(db)
        plan_repo = PlanRepository(db)

        invoice = inv_repo.get(args.invoice_id)
        if not invoice:
            print(f"Invoice #{args.invoice_id} not found.", file=sys.stderr)
            return 1
            
        invoice.line_items = line_repo.list_for_invoice(invoice.id)
        subscription = sub_repo.get(invoice.subscription_id)
        customer = cust_repo.get(subscription.customer_id) if subscription else None
        plan = plan_repo.get(subscription.plan_id) if subscription else None
        
        cust_name = customer.name if customer else "Unknown"
        plan_name = plan.name if plan else "Unknown"
        
        print(format_invoice_text(invoice, cust_name, plan_name))
        return 0

    if args.cmd == "upgrade":
        as_of = date.fromisoformat(args.date) if args.date else date.today()
        def mock_factory(x): return FlatRate(Money(1000, "INR"))
        def mock_discount(x): return None
        def mock_tax(c): return (GSTCalculator(Decimal("0.09"), Decimal("0.09"), Decimal("0.18")), TaxContext(customer_state=c.tax_state, seller_state="KA"))

        engine = BillingCycle(
            db, CustomerRepository(db), PlanRepository(db), SubscriptionRepository(db),
            UsageRecordRepository(db), InvoiceRepository(db), InvoiceLineItemRepository(db),
            LedgerRepository(db), mock_factory, mock_discount, mock_tax
        )
        engine.upgrade_subscription(args.subscription_id, args.new_plan_id, as_of)
        print(f"Subscription #{args.subscription_id} upgraded to plan #{args.new_plan_id} successfully.")
        return 0

    if args.cmd == "demo":
        return run_demo()

    return 0


def run_demo() -> int:
    db = Database(DB_PATH)
    db.init_schema()
    
    cust_repo = CustomerRepository(db)
    plan_repo = PlanRepository(db)
    sub_repo = SubscriptionRepository(db)
    inv_repo = InvoiceRepository(db)
    
    customer = Customer(id=None, name="Demo User", email="demo@example.com", currency="INR", tax_country="IN", tax_state="KA")
    cust_repo.add(customer)
    
    plan = Plan(id=None, name="Standard Flat Plan", pricing_type=PricingType.FLAT, billing_period=BillingPeriod.MONTHLY, base_price=Money(1500, "INR"), currency="INR", usage_metric="seats")
    plan_repo.add(plan)
    
    start_date = date.today()
    sub_model = Subscription(
        id=None, customer_id=customer.id, plan_id=plan.id, status=SubscriptionStatus.ACTIVE,
        current_period_start=start_date, current_period_end=start_date + timedelta(days=30),
        trial_end=None, past_due_since=None
    )
    sub_repo.add(sub_model)
    
    def mock_factory(x): return FlatRate(Money(1500, "INR"))
    def mock_discount(x): return None
    def mock_tax(c): return (GSTCalculator(Decimal("0.09"), Decimal("0.09"), Decimal("0.18")), TaxContext(customer_state=c.tax_state, seller_state="KA"))

    engine = BillingCycle(
        db, cust_repo, plan_repo, sub_repo,
        UsageRecordRepository(db), inv_repo, InvoiceLineItemRepository(db),
        LedgerRepository(db), mock_factory, mock_discount, mock_tax
    )
    
    result = engine.run(start_date + timedelta(days=31))
    
    print("--- DEMO SCENARIO EXECUTED ---")
    print(f"Created Customer ID: {customer.id}")
    print(f"Created Plan ID: {plan.id}")
    print(f"Created Subscription ID: {sub_model.id}")
    print(f"Invoices Generated during run: {result.invoices_created}")
    return 0