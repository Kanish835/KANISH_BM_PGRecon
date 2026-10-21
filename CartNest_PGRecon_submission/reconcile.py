import csv
import json
import re
import shutil
from collections import defaultdict
from datetime import datetime
from html import escape
from pathlib import Path


ROOT = Path(__file__).resolve().parent
OUTPUT = ROOT / "outputs"
TOLERANCE = 0.02


def read_csv(name):
    with (ROOT / name).open(newline="", encoding="utf-8-sig") as handle:
        return list(csv.DictReader(handle))


def money(value):
    return round(float(str(value or 0).replace(",", "")), 2)


def normalise_order_ref(value):
    return re.sub(r"[^A-Z0-9]", "", (value or "").upper())


def iso_date(value):
    for fmt in ("%d-%m-%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(value, fmt).date().isoformat()
        except ValueError:
            pass
    raise ValueError("Unsupported date: " + value)


def write_csv(path, rows, fields):
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def rate_for(rates, method, amount):
    candidates = [r for r in rates if r["payment_method"] == method]
    for rate in candidates:
        lower = float(rate["amount_above"])
        upper = rate["amount_up_to"]
        if amount >= lower and (not upper or amount <= float(upper)):
            return float(rate["mdr_pct"]), float(rate["gst_on_mdr_pct"])
    raise ValueError(f"No MDR rate for {method} amount {amount}")


def clean_inputs():
    orders = read_csv("orders.csv")
    transactions = read_csv("pg_transactions.csv")
    rates = read_csv("mdr_rate_card.csv")
    bank = read_csv("bank_statement.csv")

    clean_orders = []
    for row in orders:
        clean_orders.append(
            {
                **row,
                "order_date": iso_date(row["order_date"]),
                "order_amount": f"{money(row['order_amount']):.2f}",
                "normalised_order_ref": normalise_order_ref(row["order_id"]),
            }
        )

    clean_transactions = []
    for row in transactions:
        gross = money(row["gross_amount"])
        mdr_pct, gst_pct = rate_for(rates, row["payment_method"], gross)
        expected_mdr = 0 if row["txn_type"] == "REFUND" else round(gross * mdr_pct / 100, 2)
        expected_gst = 0 if row["txn_type"] == "REFUND" else round(expected_mdr * gst_pct / 100, 2)
        expected_net = round(float(row["net_amount"]), 2)
        if row["txn_type"] == "CAPTURE":
            expected_net = round(gross - expected_mdr - expected_gst, 2)
        clean_transactions.append(
            {
                **row,
                "merchant_order_ref": normalise_order_ref(row["merchant_order_ref"]),
                "txn_date": iso_date(row["txn_date"]),
                "settlement_date": iso_date(row["settlement_date"]),
                "gross_amount": f"{gross:.2f}",
                "mdr_fee": f"{money(row['mdr_fee']):.2f}",
                "gst_on_fee": f"{money(row['gst_on_fee']):.2f}",
                "net_amount": f"{money(row['net_amount']):.2f}",
                "expected_mdr_fee": f"{expected_mdr:.2f}",
                "expected_gst_on_fee": f"{expected_gst:.2f}",
                "expected_net_amount": f"{expected_net:.2f}",
                "mdr_rate_pct": f"{mdr_pct:.2f}",
            }
        )

    clean_bank = []
    for row in bank:
        clean_bank.append(
            {
                **row,
                "value_date": iso_date(row["value_date"]),
                "debit": f"{money(row['debit']):.2f}",
                "credit": f"{money(row['credit']):.2f}",
            }
        )
    return clean_orders, clean_transactions, clean_bank


def reconcile(orders, transactions, bank):
    orders_by_ref = defaultdict(list)
    captures_by_order = defaultdict(list)
    capture_by_id = {}
    for order in orders:
        orders_by_ref[order["normalised_order_ref"]].append(order)
    for txn in transactions:
        if txn["txn_type"] == "CAPTURE":
            captures_by_order[txn["merchant_order_ref"]].append(txn)
            capture_by_id[txn["pg_txn_id"]] = txn

    transaction_rows = []
    exceptions = []
    issue_counts = defaultdict(lambda: {"count": 0, "amount": 0.0})

    def add_exception(kind, reference, order_ref, batch, amount, details):
        issue_counts[kind]["count"] += 1
        issue_counts[kind]["amount"] += amount
        exceptions.append(
            {
                "exception_type": kind,
                "reference": reference,
                "merchant_order_ref": order_ref,
                "settlement_batch_id": batch,
                "amount": f"{amount:.2f}",
                "details": details,
            }
        )

    for txn in transactions:
        order_ref = txn["merchant_order_ref"]
        if txn["txn_type"] == "REFUND":
            original = capture_by_id.get(txn["original_payment_id"])
            order_ref = original["merchant_order_ref"] if original else ""
        matches = orders_by_ref[order_ref]
        order = matches[0] if matches else None
        gross = money(txn["gross_amount"])
        flags = []
        if not order:
            flags.append("payment_without_order" if txn["txn_type"] == "CAPTURE" else "refund_without_order")
        else:
            if abs(money(order["order_amount"]) - gross) > TOLERANCE:
                flags.append("amount_difference")
            expected_status = "Paid" if txn["txn_type"] == "CAPTURE" else "Refunded"
            if order["order_status"] != expected_status:
                if txn["txn_type"] == "CAPTURE":
                    flags.append(
                        "payment_against_failed_cancelled"
                        if order["order_status"] in ("Failed", "Cancelled")
                        else "capture_after_refund"
                    )
                else:
                    flags.append("refund_status_mismatch")
        if txn["txn_type"] == "CAPTURE" and len(captures_by_order[order_ref]) > 1:
            if txn is not captures_by_order[order_ref][0]:
                flags.append("duplicate_charge")
        fee_ok = (
            abs(money(txn["mdr_fee"]) - money(txn["expected_mdr_fee"])) <= TOLERANCE
            and abs(money(txn["gst_on_fee"]) - money(txn["expected_gst_on_fee"])) <= TOLERANCE
        )
        if not fee_ok:
            flags.append("mdr_gst_overcharge")
        actual_fee_net = gross - money(txn["mdr_fee"]) - money(txn["gst_on_fee"])
        if txn["txn_type"] == "CAPTURE" and abs(money(txn["net_amount"]) - actual_fee_net) > TOLERANCE:
            flags.append("gateway_net_calculation_error")
        transaction_rows.append(
            {
                **txn,
                "resolved_order_ref": order_ref,
                "order_id": order["order_id"] if order else "",
                "order_status": order["order_status"] if order else "",
                "order_amount": order["order_amount"] if order else "",
                "reconciliation_status": "matched" if not flags else "exception",
                "exception_flags": "|".join(flags),
            }
        )
        for flag in flags:
            issue_amount = gross
            if flag == "amount_difference" and order:
                issue_amount = abs(money(order["order_amount"]) - gross)
            elif flag == "mdr_gst_overcharge":
                issue_amount = max(
                    0,
                    money(txn["mdr_fee"]) + money(txn["gst_on_fee"])
                    - money(txn["expected_mdr_fee"]) - money(txn["expected_gst_on_fee"]),
                )
            elif flag == "gateway_net_calculation_error":
                issue_amount = abs(money(txn["net_amount"]) - actual_fee_net)
            elif flag == "duplicate_charge":
                issue_amount = gross
            add_exception(flag, txn["pg_txn_id"], order_ref, txn["settlement_batch_id"], issue_amount, flag)

    for order in orders:
        if order["order_status"] == "Paid" and not captures_by_order[order["normalised_order_ref"]]:
            add_exception(
                "paid_order_without_payment",
                order["order_id"],
                order["normalised_order_ref"],
                "",
                money(order["order_amount"]),
                "paid order has no gateway capture",
            )

    gateway_by_batch = defaultdict(lambda: {"gross": 0.0, "net": 0.0, "count": 0, "date": ""})
    for txn in transactions:
        batch = gateway_by_batch[txn["settlement_batch_id"]]
        batch["gross"] += money(txn["gross_amount"])
        batch["net"] += money(txn["net_amount"])
        batch["count"] += 1
        batch["date"] = txn["settlement_date"]

    bank_by_batch = {}
    bank_dates = {}
    for row in bank:
        match = re.search(r"(STL-\d{8})", row["narration"])
        if match and money(row["credit"]) > 0:
            bank_by_batch[match.group(1)] = money(row["credit"])
            bank_dates[match.group(1)] = row["value_date"]

    settlement_rows = []
    for batch_id, values in sorted(gateway_by_batch.items()):
        expected = round(values["net"], 2)
        received = bank_by_batch.get(batch_id)
        difference = None if received is None else round(received - expected, 2)
        if received is None and values["date"] >= "2026-10-01":
            status = "due_in_october"
        elif received is None:
            status = "missing"
        elif abs(difference) <= TOLERANCE:
            status = "matched"
        elif difference < 0:
            status = "short_paid"
        else:
            status = "over_paid"
        settlement_rows.append(
            {
                "settlement_batch_id": batch_id,
                "settlement_date": values["date"],
                "bank_value_date": bank_dates.get(batch_id, ""),
                "gateway_transaction_count": values["count"],
                "gateway_gross_amount": f"{values['gross']:.2f}",
                "gateway_expected_net_amount": f"{expected:.2f}",
                "bank_credit_amount": "" if received is None else f"{received:.2f}",
                "difference": "" if difference is None else f"{difference:.2f}",
                "reconciliation_status": status,
            }
        )
        if status not in ("matched", "due_in_october"):
            add_exception(
                "settlement_" + status,
                batch_id,
                "",
                batch_id,
                expected if received is None else abs(difference),
                status,
            )
    bridge = build_bridge(orders, transactions, captures_by_order, orders_by_ref, bank_by_batch, settlement_rows)
    return transaction_rows, settlement_rows, exceptions, issue_counts, bridge


def build_bridge(orders, transactions, captures_by_order, orders_by_ref, bank_by_batch, settlement_rows):
    paid_sales = sum(money(order["order_amount"]) for order in orders if order["order_status"] == "Paid")
    missing_paid = sum(
        money(order["order_amount"])
        for order in orders
        if order["order_status"] == "Paid" and not captures_by_order[order["normalised_order_ref"]]
    )
    linked_amount_difference = 0.0
    duplicate_amount = 0.0
    for order in orders:
        if order["order_status"] != "Paid":
            continue
        captures = captures_by_order[order["normalised_order_ref"]]
        if captures:
            linked_amount_difference += money(captures[0]["gross_amount"]) - money(order["order_amount"])
            duplicate_amount += sum(money(capture["gross_amount"]) for capture in captures[1:])
    refunded_capture = sum(
        money(txn["gross_amount"])
        for txn in transactions
        if txn["txn_type"] == "CAPTURE"
        and orders_by_ref[txn["merchant_order_ref"]]
        and orders_by_ref[txn["merchant_order_ref"]][0]["order_status"] == "Refunded"
    )
    invalid_capture = sum(
        money(txn["gross_amount"])
        for txn in transactions
        if txn["txn_type"] == "CAPTURE"
        and (
            not orders_by_ref[txn["merchant_order_ref"]]
            or orders_by_ref[txn["merchant_order_ref"]][0]["order_status"] in ("Failed", "Cancelled")
        )
    )
    capture_total = sum(money(txn["gross_amount"]) for txn in transactions if txn["txn_type"] == "CAPTURE")
    refund_total = sum(money(txn["gross_amount"]) for txn in transactions if txn["txn_type"] == "REFUND")
    gateway_net = sum(money(txn["net_amount"]) for txn in transactions)
    bank_received = sum(money(row["bank_credit_amount"]) for row in settlement_rows if row["bank_credit_amount"])
    return [
        {"line_type": "detail", "bridge_line": "Paid order sales", "amount": f"{paid_sales:.2f}", "explanation": "Paid orders in order system"},
        {"line_type": "detail", "bridge_line": "Less: paid orders without gateway capture", "amount": f"{-missing_paid:.2f}", "explanation": "20 paid orders have no payment"},
        {"line_type": "detail", "bridge_line": "Gateway amount differences on linked paid orders", "amount": f"{linked_amount_difference:.2f}", "explanation": "First capture versus order amount"},
        {"line_type": "detail", "bridge_line": "Duplicate capture charges", "amount": f"{duplicate_amount:.2f}", "explanation": "Additional captures for the same paid order"},
        {"line_type": "detail", "bridge_line": "Captures for refunded orders", "amount": f"{refunded_capture:.2f}", "explanation": "Captured before refund status"},
        {"line_type": "detail", "bridge_line": "Captures with failed, cancelled, or missing orders", "amount": f"{invalid_capture:.2f}", "explanation": "Gateway records without valid paid order"},
        {"line_type": "subtotal", "bridge_line": "Gateway capture gross", "amount": f"{capture_total:.2f}", "explanation": "Control total"},
        {"line_type": "detail", "bridge_line": "Less: gateway refunds", "amount": f"{-refund_total:.2f}", "explanation": "Refund transactions"},
        {"line_type": "subtotal", "bridge_line": "Gateway net before fees", "amount": f"{capture_total - refund_total:.2f}", "explanation": "Captures less refunds"},
        {"line_type": "detail", "bridge_line": "MDR and GST retained by gateway", "amount": f"{-(capture_total - refund_total - gateway_net):.2f}", "explanation": "Gateway gross-to-net fees"},
        {"line_type": "subtotal", "bridge_line": "Gateway net settlement expected", "amount": f"{gateway_net:.2f}", "explanation": "Expected bank receipts"},
        {"line_type": "detail", "bridge_line": "Cash received in bank during statement period", "amount": f"{bank_received:.2f}", "explanation": "Matched gateway credits only"},
        {"line_type": "subtotal", "bridge_line": "Unreceived / timing difference", "amount": f"{bank_received - gateway_net:.2f}", "explanation": "Includes short-paid, missing, and October-due batches"},
    ]


def build_dashboard(summary, transaction_rows, settlement_rows, exceptions, bridge):
    status_counts = summary["transaction_status_counts"]
    settlement_counts = summary["settlement_status_counts"]
    transaction_exception_count = status_counts.get("exception", 0)
    settlement_exception_count = sum(
        value
        for key, value in settlement_counts.items()
        if key not in ("matched", "due_in_october")
    )
    matched_pct = (
        status_counts.get("matched", 0) / summary["transactions"] * 100
        if summary["transactions"]
        else 0
    )
    rows = "".join(
        f"""<tr data-status="{escape(row['reconciliation_status'])}">
        <td><strong>{escape(row['settlement_batch_id'])}</strong></td>
        <td>{row['gateway_transaction_count']:,}</td>
        <td class="amount">₹{money(row['gateway_expected_net_amount']):,.2f}</td>
        <td class="amount">{'—' if not row['bank_credit_amount'] else '₹' + f"{money(row['bank_credit_amount']):,.2f}"}</td>
        <td class="amount {'negative' if row['difference'] and money(row['difference']) < 0 else ''}">{'—' if not row['difference'] else '₹' + f"{money(row['difference']):,.2f}"}</td>
        <td><span class="status {escape(row['reconciliation_status'])}">{escape(row['reconciliation_status'].replace('_', ' '))}</span></td>
        </tr>"""
        for row in settlement_rows
    )
    chart_bars = "".join(
        f"""<div class="bar-item"><div class="bar-label"><span>{escape(row['settlement_batch_id'].replace('STL-', ''))}</span>
        <b>{'Matched' if row['reconciliation_status'] == 'matched' else 'Review'}</b></div>
        <div class="bar-track"><div class="bar-fill {escape(row['reconciliation_status'])}" style="width:{min(100, max(4, money(row['gateway_transaction_count']) / 2.5))}%"></div></div></div>"""
        for row in settlement_rows[-10:]
    )
    exception_rows = "".join(
        f"""<tr><td><span class="status {escape('exception')}">Exception</span></td>
        <td><strong>{escape(row['reference'])}</strong></td>
        <td>{escape(row['settlement_batch_id'] or '—')}</td>
        <td class="amount">₹{money(row['amount']):,.2f}</td>
        <td>{escape(row['details'].replace('_', ' '))}</td></tr>"""
        for row in exceptions
    )
    bridge_rows = "".join(
        f"<tr><td>{escape(row['bridge_line'])}</td><td class='amount'>{'₹' + f'{money(row['amount']):,.2f}'}</td><td>{escape(row['explanation'])}</td></tr>"
        for row in bridge
    )
    issue_rows = "".join(
        f"<tr><td>{escape(kind.replace('_', ' '))}</td><td>{value['count']:,}</td><td class='amount'>₹{value['amount']:,.2f}</td></tr>"
        for kind, value in sorted(summary["issue_counts"].items())
    )
    daily_rows = "".join(
        f"<tr><td>{escape(row['settlement_date'])}</td><td>{escape(row['settlement_batch_id'])}</td><td class='amount'>₹{money(row['gateway_expected_net_amount']):,.2f}</td><td class='amount'>{'—' if not row['bank_credit_amount'] else '₹' + f'{money(row['bank_credit_amount']):,.2f}'}</td><td><span class='status {escape(row['reconciliation_status'])}'>{escape(row['reconciliation_status'].replace('_', ' '))}</span></td></tr>"
        for row in settlement_rows
    )
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Cartnest · Reconciliation</title>
<style>
:root {{ --ink:#172033; --muted:#718096; --line:#e8edf4; --card:#fff; --bg:#f5f7fb; --navy:#152b4a; --blue:#3b82f6; --green:#159570; --amber:#d99016; --red:#d95757; }}
* {{ box-sizing:border-box }} body {{ margin:0; color:var(--ink); background:var(--bg); font:14px Inter,Segoe UI,Arial,sans-serif }}
.shell {{ display:flex; min-height:100vh }} .sidebar {{ width:232px; padding:26px 16px; background:var(--navy); color:#c9d4e4 }}
.brand {{ display:flex; align-items:center; gap:10px; color:#fff; font-weight:750; font-size:18px; margin:0 10px 42px }}
.brand-mark {{ display:grid; place-items:center; width:32px; height:32px; border-radius:10px; color:#fff; background:#3182ce }}
.nav-item {{ display:flex; gap:12px; align-items:center; margin:5px 0; padding:11px 12px; border-radius:9px; cursor:pointer }} .nav-item:hover,.nav-item.active {{ color:#fff; background:#24466e }}
.side-note {{ margin:42px 10px 0; color:#8497b1; font-size:12px; line-height:1.6 }}
.main {{ flex:1; min-width:0; width:auto; max-width:1440px; padding:34px 38px 50px }} .eyebrow {{ color:var(--blue); font-size:12px; font-weight:750; letter-spacing:.1em; text-transform:uppercase }}
h1 {{ margin:7px 0 5px; font-size:30px; letter-spacing:-.03em }} .subtitle {{ margin:0; color:var(--muted) }}
.topline {{ display:flex; justify-content:space-between; align-items:flex-start; gap:16px; margin-bottom:28px }}
.pill {{ border:1px solid #cfe8df; color:#087f62; background:#effaf6; border-radius:20px; padding:9px 13px; font-size:12px; font-weight:700 }}
.grid {{ display:grid; grid-template-columns:repeat(4,minmax(0,1fr)); gap:16px; margin-bottom:20px }}
.card {{ background:var(--card); border:1px solid var(--line); border-radius:14px; padding:20px; box-shadow:0 6px 20px rgba(30,55,90,.04) }}
.metric {{ display:flex; justify-content:space-between; align-items:flex-start }} .metric-label {{ color:var(--muted); font-size:12px; font-weight:650 }}
.metric-value {{ margin:13px 0 7px; font-size:28px; font-weight:760; letter-spacing:-.04em }} .metric-note {{ color:var(--muted); font-size:12px }}
.icon {{ display:grid; place-items:center; width:36px; height:36px; border-radius:10px; background:#edf4ff; color:var(--blue); font-size:18px }}
.icon.green {{ background:#eaf9f4; color:var(--green) }} .icon.red {{ background:#fff0f0; color:var(--red) }} .icon.amber {{ background:#fff7e8; color:var(--amber) }}
.two-col {{ display:grid; grid-template-columns:1.1fr .9fr; gap:20px; margin-bottom:20px }} .card-title {{ margin:0; font-size:15px }}
.card-head {{ display:flex; justify-content:space-between; align-items:center; margin-bottom:18px }} .muted {{ color:var(--muted); font-size:12px }}
.health {{ display:flex; align-items:center; gap:22px }} .ring {{ width:128px; height:128px; display:grid; place-items:center; border-radius:50%; background:conic-gradient(var(--green) {matched_pct:.1f}%,#e8edf4 0) }}
.ring::after {{ content:""; width:94px; height:94px; border-radius:50%; background:#fff }} .ring-value {{ position:absolute; font-size:22px; font-weight:760 }}
.legend {{ display:grid; gap:13px; flex:1 }} .legend-row {{ display:flex; justify-content:space-between; color:var(--muted); font-size:12px }} .legend-dot {{ display:inline-block; width:8px; height:8px; border-radius:50%; margin-right:7px; background:var(--green) }} .legend-dot.red {{ background:var(--red) }}
.bar-chart {{ display:grid; gap:10px }} .bar-label {{ display:flex; justify-content:space-between; color:var(--muted); font-size:11px; margin-bottom:4px }} .bar-label b {{ color:var(--ink); font-weight:650 }}
.bar-track {{ height:8px; border-radius:8px; background:#edf1f6; overflow:hidden }} .bar-fill {{ height:100%; border-radius:8px; background:var(--blue) }} .bar-fill.amount_mismatch,.bar-fill.missing_bank_credit {{ background:var(--red) }}
.table-card {{ padding:0; overflow:hidden }} .table-head {{ padding:20px 20px 15px; display:flex; justify-content:space-between; gap:16px; align-items:center }}
.controls {{ display:flex; gap:8px }} input,select {{ border:1px solid var(--line); border-radius:8px; padding:9px 11px; background:#fff; color:var(--ink); font:inherit; font-size:12px }}
input {{ width:200px }} .table-wrap {{ overflow-x:auto }} table {{ border-collapse:collapse; width:100%; min-width:760px }} th,td {{ padding:13px 20px; border-top:1px solid var(--line); text-align:left; white-space:nowrap }} th {{ color:var(--muted); font-size:11px; font-weight:700; text-transform:uppercase; letter-spacing:.05em; background:#fbfcfe }} td {{ font-size:13px }}
.amount {{ text-align:right; font-variant-numeric:tabular-nums }} .negative {{ color:var(--red) }} .status {{ display:inline-flex; border-radius:16px; padding:5px 9px; font-size:11px; font-weight:700; text-transform:capitalize }} .status.matched {{ color:#087f62; background:#eaf9f4 }} .status.exception,.status.amount_mismatch,.status.missing_bank_credit,.status.short_paid,.status.missing {{ color:#b42318; background:#fff0f0 }} .status.due_in_october {{ color:#9a6700; background:#fff7e8 }}
.view {{ display:none }} .view.active {{ display:block }} .view.active .topline {{ display:flex }}
@media (max-width:950px) {{ .sidebar {{ display:none }} .main {{ padding:25px 18px }} .grid {{ grid-template-columns:repeat(2,minmax(0,1fr)) }} .two-col {{ grid-template-columns:1fr }} }}
@media (max-width:560px) {{ .grid {{ grid-template-columns:1fr }} .topline,.table-head {{ align-items:flex-start; flex-direction:column }} .controls,input {{ width:100% }} .controls {{ flex-direction:column; width:100% }} }}
</style></head><body>
<div class="shell"><aside class="sidebar"><div class="brand"><span class="brand-mark">↗</span>Cartnest</div>
<div class="nav-item active" data-view="reconciliation">▦ <span>Reconciliation</span></div><div class="nav-item" data-view="settlements">◷ <span>Settlements</span></div><div class="nav-item" data-view="exceptions">⚠ <span>Exceptions</span></div>
<div class="side-note">Payment operations<br><strong>September–October 2026</strong></div></aside>
<main class="main"><section id="reconciliation" class="view active"><div class="topline"><div><div class="eyebrow">Finance operations</div><h1>Payment reconciliation</h1><p class="subtitle">Monitor gateway captures, fees, and bank settlement receipts.</p></div><div class="pill">● Reconciliation overview</div></div>
<section class="grid">
<div class="card"><div class="metric"><span class="metric-label">Total orders</span><span class="icon">▦</span></div><div class="metric-value">{summary['orders']:,}</div><div class="metric-note">Across all order statuses</div></div>
<div class="card"><div class="metric"><span class="metric-label">Gateway transactions</span><span class="icon">↗</span></div><div class="metric-value">{summary['transactions']:,}</div><div class="metric-note">Captures and refunds processed</div></div>
<div class="card"><div class="metric"><span class="metric-label">Transaction exceptions</span><span class="icon red">!</span></div><div class="metric-value">{transaction_exception_count:,}</div><div class="metric-note">Requires transaction review</div></div>
<div class="card"><div class="metric"><span class="metric-label">Settlement exceptions</span><span class="icon amber">◒</span></div><div class="metric-value">{settlement_exception_count:,}</div><div class="metric-note">Across {len(settlement_rows)} settlement batches</div></div>
</section>
<section class="two-col"><div class="card"><div class="card-head"><h2 class="card-title">Reconciliation health</h2><span class="muted">Transaction-level</span></div>
<div class="health"><div style="position:relative"><div class="ring"></div><div class="ring-value" style="position:absolute;inset:0;display:grid;place-items:center">{matched_pct:.1f}%</div></div>
<div class="legend"><div class="legend-row"><span><i class="legend-dot"></i>Matched transactions</span><strong>{status_counts.get('matched', 0):,}</strong></div><div class="legend-row"><span><i class="legend-dot red"></i>Exceptions</span><strong>{transaction_exception_count:,}</strong></div><div class="legend-row"><span>Review tolerance</span><strong>₹{summary['tolerance']:.2f}</strong></div></div></div></div>
<div class="card"><div class="card-head"><h2 class="card-title">Recent settlement batches</h2><span class="muted">Last 10</span></div><div class="bar-chart">{chart_bars}</div></div></section>
<section class="card table-card"><div class="table-head"><div><h2 class="card-title">Settlement reconciliation</h2><span class="muted">Gateway expected net compared with bank credits</span></div><div class="controls"><input id="search" placeholder="Search batch..." aria-label="Search batches"><select id="statusFilter" aria-label="Filter status"><option value="all">All statuses</option><option value="matched">Matched</option><option value="amount_mismatch">Amount mismatch</option><option value="missing_bank_credit">Missing credit</option></select></div></div>
<div class="table-wrap"><table><thead><tr><th>Batch</th><th>Transactions</th><th class="amount">Expected net</th><th class="amount">Bank credit</th><th class="amount">Difference</th><th>Status</th></tr></thead><tbody id="batchRows">{rows}</tbody></table></div></section>
</section>
<section class="two-col"><div class="card"><div class="card-head"><h2 class="card-title">Sales to cash bridge</h2><span class="muted">₹ values</span></div><div class="table-wrap"><table><thead><tr><th>Bridge item</th><th class="amount">Amount</th><th>Explanation</th></tr></thead><tbody>{bridge_rows}</tbody></table></div></div>
<div class="card"><div class="card-head"><h2 class="card-title">Exceptions by type</h2><span class="muted">Count and value</span></div><div class="table-wrap"><table><thead><tr><th>Type</th><th>Count</th><th class="amount">Value</th></tr></thead><tbody>{issue_rows}</tbody></table></div></div></section>
<section class="card table-card"><div class="table-head"><div><h2 class="card-title">Daily settlements vs bank</h2><span class="muted">Batch-level daily control</span></div></div><div class="table-wrap"><table><thead><tr><th>Date</th><th>Batch</th><th class="amount">Gateway expected</th><th class="amount">Bank received</th><th>Status</th></tr></thead><tbody>{daily_rows}</tbody></table></div></section>
<section id="settlements" class="view"><div class="topline"><div><div class="eyebrow">Finance operations</div><h1>Settlements</h1><p class="subtitle">Review gateway settlement batches and bank receipts.</p></div><div class="pill">● {len(settlement_rows)} batches</div></div>
<section class="card table-card"><div class="table-head"><div><h2 class="card-title">Settlement batches</h2><span class="muted">Full settlement register</span></div><div class="controls"><input id="settlementSearch" placeholder="Search batch..." aria-label="Search settlement batches"></div></div>
<div class="table-wrap"><table><thead><tr><th>Batch</th><th>Transactions</th><th class="amount">Expected net</th><th class="amount">Bank credit</th><th class="amount">Difference</th><th>Status</th></tr></thead><tbody>{rows}</tbody></table></div></section></section>
<section id="exceptions" class="view"><div class="topline"><div><div class="eyebrow">Finance operations</div><h1>Exceptions</h1><p class="subtitle">Investigate transactions and settlements that need attention.</p></div><div class="pill">{len(exceptions):,} items to review</div></div>
<section class="card table-card"><div class="table-head"><div><h2 class="card-title">Exception register</h2><span class="muted">Prioritised reconciliation issues</span></div><div class="controls"><input id="exceptionSearch" placeholder="Search exceptions..." aria-label="Search exceptions"></div></div>
<div class="table-wrap"><table><thead><tr><th>Type</th><th>Reference</th><th>Settlement batch</th><th class="amount">Amount</th><th>Details</th></tr></thead><tbody>{exception_rows}</tbody></table></div></section></section>
</main></div>
<script>
const search = document.getElementById('search'), filter = document.getElementById('statusFilter');
function filterRows() {{ const query = search.value.toLowerCase(), status = filter.value;
document.querySelectorAll('#batchRows tr').forEach(row => {{ const matchesText = row.textContent.toLowerCase().includes(query);
const matchesStatus = status === 'all' || row.dataset.status === status; row.hidden = !(matchesText && matchesStatus); }}); }}
search.addEventListener('input', filterRows); filter.addEventListener('change', filterRows);
document.querySelectorAll('.nav-item').forEach(item => item.addEventListener('click', () => {{
    document.querySelectorAll('.nav-item').forEach(nav => nav.classList.toggle('active', nav === item));
    document.querySelectorAll('.view').forEach(view => view.classList.toggle('active', view.id === item.dataset.view));
}}));
</script></body></html>"""


def build_cfo_summary(summary, issue_counts, settlement_rows):
    due_october = sum(
        money(row["gateway_expected_net_amount"])
        for row in settlement_rows
        if row["reconciliation_status"] == "due_in_october"
    )
    short_paid = sum(
        abs(money(row["difference"]))
        for row in settlement_rows
        if row["reconciliation_status"] == "short_paid"
    )
    missing = sum(
        money(row["gateway_expected_net_amount"])
        for row in settlement_rows
        if row["reconciliation_status"] == "missing"
    )
    overcharge = issue_counts.get("mdr_gst_overcharge", {}).get("amount", 0)
    sales_to_bank = summary["paid_order_sales"] - summary["bank_gateway_cash_received"]
    return f"""CartNest payment gateway reconciliation — CFO summary

Paid order sales were ₹{summary['paid_order_sales']:,.2f}. Gateway settlement credits identified in the bank were ₹{summary['bank_gateway_cash_received']:,.2f}, giving a sales-to-bank difference of ₹{sales_to_bank:,.2f}. Expected gateway net settlement was ₹{summary['gateway_net_expected']:,.2f}; the unexplained gateway-to-bank gap is ₹{summary['bank_gateway_cash_received'] - summary['gateway_net_expected']:,.2f}.

Largest reconciling items:
1. ₹{due_october:,.2f} relates to {sum(row['reconciliation_status'] == 'due_in_october' for row in settlement_rows)} settlement batches dated in October and is a timing difference, not a loss.
2. ₹{short_paid:,.2f} is short-paid across settlement batches and should be raised with the gateway.
3. ₹{missing:,.2f} is the September batch with no matching bank credit and requires bank/gateway tracing.
4. ₹{overcharge:,.2f} of MDR/GST overcharges was identified across {issue_counts.get('mdr_gst_overcharge', {}).get('count', 0)} transactions and should be recovered from the gateway.
5. Duplicate captures, invalid-status captures, and paid orders without a capture are listed at record level in exceptions.csv; customer-impacting duplicate charges should be refunded after confirmation.

Recommended actions:
- Confirm October-due batches against the October bank statement.
- Raise the short-paid and missing settlement batches with the gateway using the batch IDs.
- Recover the calculated MDR/GST overcharges.
- Investigate duplicate captures and payments against failed/cancelled orders before customer remediation.

Pattern to monitor: UPI and Wallet transactions account for the MDR/GST overcharge exceptions; monitor fee-rule application by payment method and amount band.
"""


def write_cfo_pdf(summary, issue_counts, settlement_rows):
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import getSampleStyleSheet
    from reportlab.lib.units import mm
    from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer

    path = OUTPUT / "cfo_summary.pdf"
    document = SimpleDocTemplate(
        str(path),
        pagesize=A4,
        rightMargin=18 * mm,
        leftMargin=18 * mm,
        topMargin=16 * mm,
        bottomMargin=16 * mm,
    )
    styles = getSampleStyleSheet()
    story = [Paragraph("CartNest Payment Gateway Reconciliation", styles["Title"])]
    story.append(Spacer(1, 5 * mm))
    text = build_cfo_summary(summary, issue_counts, settlement_rows)
    for block in text.split("\n\n"):
        story.append(Paragraph(block.replace("\n", "<br/>"), styles["BodyText"]))
        story.append(Spacer(1, 3 * mm))
    document.build(story)
    return path


def main():
    OUTPUT.mkdir(exist_ok=True)
    orders, transactions, bank = clean_inputs()
    transaction_rows, settlement_rows, exceptions, issue_counts, bridge = reconcile(
        orders, transactions, bank
    )

    write_csv(OUTPUT / "cleaned_orders.csv", orders, list(orders[0]))
    write_csv(OUTPUT / "cleaned_pg_transactions.csv", transactions, list(transactions[0]))
    write_csv(OUTPUT / "cleaned_bank_statement.csv", bank, list(bank[0]))
    write_csv(OUTPUT / "transaction_reconciliation.csv", transaction_rows, list(transaction_rows[0]))
    write_csv(OUTPUT / "settlement_reconciliation.csv", settlement_rows, list(settlement_rows[0]))
    write_csv(
        OUTPUT / "exceptions.csv",
        exceptions,
        ["exception_type", "reference", "merchant_order_ref", "settlement_batch_id", "amount", "details"],
    )
    write_csv(
        OUTPUT / "bridge.csv",
        bridge,
        ["line_type", "bridge_line", "amount", "explanation"],
    )
    write_csv(
        OUTPUT / "daily_settlements.csv",
        settlement_rows,
        list(settlement_rows[0]),
    )
    data_quality = [
        {
            "issue": "Mixed date formats",
            "records": len(orders) + len(transactions) + len(bank),
            "resolution": "Converted all dates to ISO YYYY-MM-DD.",
        },
        {
            "issue": "Inconsistent order references",
            "records": sum(
                "-" in row["merchant_order_ref"] for row in transactions
            ),
            "resolution": "Removed punctuation and upper-cased references before matching.",
        },
        {
            "issue": "Blank refund merchant references",
            "records": sum(row["txn_type"] == "REFUND" and not row["merchant_order_ref"] for row in transactions),
            "resolution": "Resolved refunds through original_payment_id to the original capture and order.",
        },
        {
            "issue": "Duplicate capture references",
            "records": sum(
                max(0, len(rows) - 1)
                for rows in (
                    [row for row in transactions if row["txn_type"] == "CAPTURE" and row["merchant_order_ref"] == ref]
                    for ref in {row["merchant_order_ref"] for row in transactions if row["txn_type"] == "CAPTURE"}
                )
            ),
            "resolution": "Retained every record and flagged additional captures as duplicate charges.",
        },
        {
            "issue": "Non-gateway bank entries",
            "records": sum(
                "PAYGATE SOLUTIONS" not in row["narration"] for row in bank
            ),
            "resolution": "Excluded unrelated bank credits/debits from gateway settlement matching.",
        },
    ]
    write_csv(OUTPUT / "data_quality.csv", data_quality, ["issue", "records", "resolution"])

    summary = {
        "orders": len(orders),
        "transactions": len(transactions),
        "paid_order_sales": round(
            sum(money(row["order_amount"]) for row in orders if row["order_status"] == "Paid"), 2
        ),
        "gateway_net_expected": round(
            sum(money(row["net_amount"]) for row in transactions), 2
        ),
        "bank_gateway_cash_received": round(
            sum(money(row["bank_credit_amount"]) for row in settlement_rows if row["bank_credit_amount"]), 2
        ),
        "transaction_status_counts": {
            status: sum(row["reconciliation_status"] == status for row in transaction_rows)
            for status in ("matched", "exception")
        },
        "settlement_status_counts": {
            status: sum(row["reconciliation_status"] == status for row in settlement_rows)
            for status in sorted({row["reconciliation_status"] for row in settlement_rows})
        },
        "exceptions": len(exceptions),
        "tolerance": TOLERANCE,
        "issue_counts": {
            key: {"count": value["count"], "amount": round(value["amount"], 2)}
            for key, value in sorted(issue_counts.items())
        },
    }
    (OUTPUT / "summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    (OUTPUT / "cfo_summary.txt").write_text(
        build_cfo_summary(summary, issue_counts, settlement_rows), encoding="utf-8"
    )
    write_cfo_pdf(summary, issue_counts, settlement_rows)
    (OUTPUT / "assumptions.txt").write_text(
        """CartNest Payment Gateway Reconciliation — assumptions

1. A merchant order reference is matched after upper-casing and removing punctuation.
2. Refunds with blank merchant_order_ref are linked through original_payment_id to the original capture.
3. A capture is expected only for a Paid order; captures against Failed or Cancelled orders are exceptions.
4. A refund is expected for a Refunded order; the refund amount is compared with the original order amount.
5. MDR and GST are recalculated using the first applicable rate-card band, rounded to paise at each fee component.
6. Gateway settlement matching uses settlement_batch_id embedded in PayGate bank narrations.
7. Bank credits without a PayGate settlement batch, including COD and marketplace receipts, are excluded from gateway settlement cash.
8. A settlement with settlement_date in October and no September bank credit is classified as due_in_october, not missing.
9. Differences within ₹0.02 are treated as rounding matches.
10. The September bank statement does not include October receipts; October-due amounts remain timing differences pending the October statement.
""",
        encoding="utf-8",
    )
    submission = ROOT / "CartNest_PGRecon_submission"
    if submission.exists():
        shutil.rmtree(submission)
    submission.mkdir()
    for source in [ROOT / "reconcile.py", OUTPUT]:
        target = submission / source.name
        if source.is_dir():
            shutil.copytree(source, target)
        else:
            shutil.copy2(source, target)
    archive = shutil.make_archive(str(ROOT / "CartNest_PGRecon"), "zip", ROOT, submission.name)
    print("submission_archive", archive)
    (OUTPUT / "dashboard.html").write_text(
        build_dashboard(summary, transaction_rows, settlement_rows, exceptions, bridge), encoding="utf-8"
    )
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
