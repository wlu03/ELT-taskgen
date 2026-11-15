def main():
    import duckdb

    from tests.test_plan_library import EVIDENCE, RELATIONSHIPS, TABLES, build_task, _load, _fetch
    from elt_taskgen.generation.populations import _effective_witnesses
    from elt_taskgen.generation.shapes import fan_out_rollup
    from elt_taskgen.generation.source_data import generate_rows
    from elt_taskgen.generation.witness import WITNESS_SAME_CHILD
    from elt_taskgen.models import AttackKind, PopulationName
    from elt_taskgen.reference.solution import compile_plan_sql
    from elt_taskgen.verification.attacks import _apply_kind
    from elt_taskgen.verification.upstream_eval import compare_mart, rows_to_canonical_csv

    print("\nSTEP 1: Build the fan-out plan")
    built = fan_out_rollup(EVIDENCE, mart="fan_out_rollup_mart")
    print(f"  Shape:   {built.shape.shape_name}")
    print(f"  Witness: {WITNESS_SAME_CHILD}")
    print("  Attack:  no_dedup")

    print("\nSTEP 2: Construct the counterfactual population")
    task = build_task(built, "proof__fan_out_rollup")
    mart = task.marts[0]
    rows = generate_rows(task, PopulationName.COUNTERFACTUAL)
    witnesses = _effective_witnesses(TABLES, built.shape, RELATIONSHIPS)
    witness_index = witnesses.index(WITNESS_SAME_CHILD)
    account_id = rows["accounts"][witness_index]["account_id"]
    for witness, account in zip(witnesses, rows["accounts"], strict=True):
        print(f"  {witness:<20} -> account {account['account_id']}")

    print(f"\nSTEP 3: Inspect the subscriptions for account {account_id}")
    subscriptions = sorted(
        (row for row in rows["subscriptions"] if row["account_id"] == account_id),
        key=lambda row: row["subscription_id"],
    )
    print(f"  {'Subscription':<14} {'Account':<10} {'Plan':<10} Amount")
    for row in subscriptions:
        print(
            f"  {row['subscription_id']:<14} {row['account_id']:<10} "
            f"{row['plan_id']:<10} {row['amount']}"
        )
    assert len(subscriptions) == 2
    assert len({row["subscription_id"] for row in subscriptions}) == 2
    assert len({row["plan_id"] for row in subscriptions}) == 1
    print("  Two different subscriptions point to the same plan.")

    print("\nSTEP 4: Compile the reference and create the mutant")
    reference_sql = compile_plan_sql(task, mart)
    mutant_sql = _apply_kind(AttackKind.NO_DEDUP, reference_sql, mart)
    assert mutant_sql is not None
    print('  Reference counts plans with: COUNT(DISTINCT "dim_key")')
    print('  Mutant counts plans with:    COUNT("dim_key")')
    print("  Both queries will use the same input rows.")

    print("\nSTEP 5: Execute both queries in DuckDB")
    with duckdb.connect(":memory:") as connection:
        _load(connection, task, rows)
        reference_rows = _fetch(connection, reference_sql)
        mutant_rows = _fetch(connection, mutant_sql)

    print(f"  {'Query':<12} {'Account':<10} {'Subscriptions':<16} Distinct plans")
    for name, result in (("Reference", reference_rows), ("Mutant", mutant_rows)):
        row = next(row for row in result if row["account_id"] == account_id)
        print(
            f"  {name:<12} {row['account_id']:<10} "
            f"{row['subscriptions_count']:<16} {row['distinct_plans_count']}"
        )
        expected = (2, 1) if name == "Reference" else (2, 2)
        assert (row["subscriptions_count"], row["distinct_plans_count"]) == expected

    print("\nSTEP 6: Compare the complete outputs against the reference")
    columns = tuple(column.name for column in mart.columns)
    gold_csv = rows_to_canonical_csv(reference_rows, columns)
    reference_matches = compare_mart(gold_csv, reference_rows, mart)
    mutant_matches = compare_mart(gold_csv, mutant_rows, mart)
    print(f"  Reference matches gold: {reference_matches}")
    print(f"  Mutant matches gold:    {mutant_matches}")
    assert reference_matches and not mutant_matches
    print("  PASS: both queries executed; the wrong plan count rejects the mutant.")


if __name__ == "__main__":
    main()
