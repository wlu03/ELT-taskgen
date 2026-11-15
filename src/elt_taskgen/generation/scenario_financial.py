"""Document and entry-level reconciliation with observable residual/status outputs."""
from elt_taskgen.generation.mart_plan import quote as q
from elt_taskgen.models import JoinType


def money(column):
    # Match the canonical warehouse's monetary precision. Cast BEFORE SUM or
    # subtraction: rounding a binary floating residual cannot recover cents.
    return f'CAST({q(column)} AS DECIMAL(38,9))'


def ledger_ops(b, e, variant):
    g = tuple(f"g_{i}" for i in range(len(e.keys)))
    parents = b.project(e.parent, ", ".join(f'{q(k)} AS {x}' for k, x in zip(e.keys, g)), "journal_entries")
    facts = b.project(e.fact, ", ".join([*(f'{q(k)} AS {x}' for k, x in zip(e.links, g)),
        f'COALESCE({money(e.debit)}, 0) AS debit_value', f'COALESCE({money(e.credit)}, 0) AS credit_value']), "journal_lines")
    distinct = "DISTINCT " if variant == "sum_distinct" else ""
    totals = b.aggregate(facts, g, {"total_debit": f'SUM({distinct}debit_value)',
        "total_credit": f'SUM({distinct}credit_value)', "line_count": "COUNT(*)"}, "entry_totals")
    keys = tuple(k for i, k in enumerate(g) if variant != f"missing_key_{i}")
    current = b.join(parents, totals, keys, ("total_debit", "total_credit", "line_count"), "complete_entries",
                     kind=JoinType.INNER if variant == "inner_parent" else JoinType.LEFT)
    current = b.project(current, ", ".join((*g, "COALESCE(total_debit,0) AS total_debit", "COALESCE(total_credit,0) AS total_credit",
        "COALESCE(line_count,0) AS line_count", "ROUND(COALESCE(total_debit,0)-COALESCE(total_credit,0),4) AS imbalance")), "entry_residuals")
    balance = "imbalance"
    if variant == "global_balance":
        scope = b.project(current, "*, 1 AS ledger_scope", "ledger_scope")
        global_total = b.aggregate(scope, ("ledger_scope",), {"global_imbalance": "SUM(imbalance)"}, "whole_ledger_total")
        current = b.join(scope, global_total, ("ledger_scope",), ("global_imbalance",), "globally_compared_entries")
        balance = "global_imbalance"
    predicate = f'{balance} <= 0.01' if variant == "ignore_credit_excess" else f'ABS({balance}) < 0.01' if variant == "exclusive_tolerance" else f'ABS({balance}) <= 0.01'
    b.project(current, ", ".join([*(f'{x} AS {q(k)}' for k, x in zip(e.keys, g)),
        "total_debit", "total_credit", "line_count", "imbalance", f'{predicate} AS is_balanced']), "mart_output")


def document_ops(b, e, variant):
    g = tuple(f"g_{i}" for i in range(len(e.keys)))
    parents = b.project(e.parent, ", ".join([*(f'{q(k)} AS {x}' for k, x in zip(e.keys, g)),
        f'COALESCE({money(e.amount)}, 0) AS document_amount']), "document_headers")
    lines = b.project(e.fact, ", ".join([*(f'{q(k)} AS {x}' for k, x in zip(e.links, g)),
        f'{money(e.debit)} AS line_value', "1 AS line_present"]), "document_lines")
    payments = b.project(e.other, ", ".join([*(f'{q(k)} AS {x}' for k, x in zip(e.other_links, g)),
        f'{money(e.other_amount)} AS payment_value', "1 AS payment_present"]), "document_payments")
    distinct = "DISTINCT " if variant == "sum_distinct" else ""
    keys = tuple(k for i, k in enumerate(g) if variant != f"missing_key_{i}")
    join_kind = JoinType.INNER if variant == "inner_parent" else JoinType.LEFT
    if variant == "raw_fanout":
        raw = b.join(parents, lines, keys, ("line_value", "line_present"), "header_lines")
        raw = b.join(raw, payments, keys, ("payment_value", "payment_present"), "raw_documents")
        current = b.aggregate(raw, (*g, "document_amount"), {"line_total": "SUM(line_value)", "paid_amount": "SUM(payment_value)",
            "line_count": "COUNT(line_present)", "payment_count": "COUNT(payment_present)"}, "raw_document_totals")
    else:
        ls = b.aggregate(lines, g, {"line_total": f'SUM({distinct}line_value)', "line_count": "COUNT(*)"}, "line_totals")
        ps = b.aggregate(payments, g, {"paid_amount": f'SUM({distinct}payment_value)', "payment_count": "COUNT(*)"}, "payment_totals")
        current = b.join(parents, ls, keys, ("line_total", "line_count"), "headers_with_lines", kind=join_kind)
        current = b.join(current, ps, keys, ("paid_amount", "payment_count"), "headers_with_payments", kind=join_kind)
    current = b.project(current, ", ".join((*g, "document_amount", "COALESCE(line_total,0) AS line_total",
        "COALESCE(paid_amount,0) AS paid_amount", "COALESCE(line_count,0) AS line_count", "COALESCE(payment_count,0) AS payment_count",
        "ROUND(document_amount-COALESCE(paid_amount,0),4) AS remaining_amount")), "document_residuals")
    b.project(current, ", ".join([*(f'{x} AS {q(k)}' for k, x in zip(e.keys, g)),
        "document_amount", "line_total", "paid_amount", "line_count", "payment_count", "remaining_amount",
        "CASE WHEN ABS(remaining_amount)<=0.01 THEN 'settled' WHEN remaining_amount<0 THEN 'overpaid' "
        "WHEN payment_count=0 THEN 'unpaid' ELSE 'outstanding' END AS payment_status"]), "mart_output")
