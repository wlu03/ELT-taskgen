"""The differential probe matrix: one scalar expression per subset rule.

`expr` is the SQL a solver would write for its destination; `only` restricts a
probe to destinations where the spelling exists. Every probe is read-only and
touches no table: the warehouses evaluate literals, so no load, no warehouse
object and no cleanup is involved.
"""
PROBES = [
    # --- portable-dbt-sql-v2 admissions -------------------------------------
    dict(id="md5_text_cast", rule="MD5 over a text cast", expr="MD5(CAST(7 AS TEXT))"),
    dict(id="md5_null", rule="MD5 of NULL", expr="MD5(CAST(NULL AS TEXT))"),
    dict(id="md5_surrogate_key", rule="dbt_utils surrogate key shape",
         expr="MD5(CAST(COALESCE(CAST(NULL AS TEXT), '_n_') || '-' || CAST(2 AS TEXT) AS TEXT))"),
    dict(id="dpipe_null", rule="|| propagates NULL", expr="CAST(NULL AS TEXT) || 'b'"),
    dict(id="dpipe_number", rule="|| over a number", expr="'n=' || CAST(2 AS INT)"),
    dict(id="concat_fn_null", rule="CONCAT with NULL", expr="CONCAT(CAST(NULL AS TEXT), 'b')"),
    dict(id="date_trunc_day_ts", rule="DATE_TRUNC day of a timestamp",
         expr="DATE_TRUNC('day', CAST('2024-03-05 10:11:12' AS TIMESTAMP))"),
    dict(id="date_trunc_month_date", rule="DATE_TRUNC month of a date",
         expr="DATE_TRUNC('month', CAST('2024-03-05' AS DATE))"),
    dict(id="extract_year", rule="EXTRACT year",
         expr="EXTRACT(year FROM CAST('2024-03-05 10:11:12' AS TIMESTAMP))"),
    dict(id="datediff_day_dates", rule="DATEDIFF day over dates",
         expr="DATEDIFF(day, CAST('2024-01-01' AS DATE), CAST('2024-03-01' AS DATE))"),
    dict(id="datediff_hour_ts", rule="DATEDIFF hour over timestamps",
         expr="DATEDIFF(hour, CAST('2024-01-01 10:59:00' AS TIMESTAMP), CAST('2024-01-01 11:01:00' AS TIMESTAMP))"),
    # --- text rendering, which every hash and concat depends on -------------
    dict(id="cast_double_text", rule="DOUBLE rendered as text", expr="CAST(CAST(1.0 AS DOUBLE) AS TEXT)"),
    dict(id="cast_third_text", rule="1/3 rendered as text", expr="CAST(CAST(1 AS DOUBLE)/CAST(3 AS DOUBLE) AS TEXT)"),
    dict(id="cast_decimal_text", rule="DECIMAL rendered as text", expr="CAST(CAST(1.50 AS DECIMAL(10,2)) AS TEXT)"),
    dict(id="cast_timestamp_text", rule="TIMESTAMP rendered as text",
         expr="CAST(CAST('2024-03-05 10:11:12' AS TIMESTAMP) AS TEXT)"),
    dict(id="cast_date_text", rule="DATE rendered as text", expr="CAST(CAST('2024-03-05' AS DATE) AS TEXT)"),
    dict(id="cast_bool_text", rule="BOOLEAN rendered as text", expr="CAST(CAST('true' AS BOOLEAN) AS TEXT)"),
    # --- arithmetic already admitted by v1 ----------------------------------
    dict(id="int_division", rule="integer / integer", expr="CAST(1 AS INT) / CAST(2 AS INT)"),
    dict(id="round_half_even", rule="ROUND of 0.5 and 2.5",
         expr="CAST(ROUND(CAST(0.5 AS DOUBLE)) AS TEXT) || ',' || CAST(ROUND(CAST(2.5 AS DOUBLE)) AS TEXT)"),
    dict(id="round_decimal_places", rule="ROUND to 3 places",
         expr="ROUND(CAST(1.0005 AS DECIMAL(10,4)), 3)"),
    dict(id="div_by_nullif_zero", rule="guarded division",
         expr="CAST(1 AS DOUBLE) / NULLIF(CAST(0 AS INT), 0)"),
    dict(id="avg_of_ints", rule="AVG over integers", expr="AVG(v)",
         from_clause="(SELECT 1 AS v UNION ALL SELECT 2) AS t"),
    dict(id="sum_decimal_scale", rule="SUM of decimals", expr="SUM(v)",
         from_clause="(SELECT CAST(0.1 AS DECIMAL(10,2)) AS v UNION ALL SELECT CAST(0.2 AS DECIMAL(10,2))) AS t"),
    # --- comparison and text functions --------------------------------------
    dict(id="string_case_equality", rule="case-sensitive equality", expr="CAST(('A' = 'a') AS TEXT)"),
    dict(id="ilike", rule="ILIKE", expr="CAST(('ABC' ILIKE 'abc') AS TEXT)"),
    dict(id="regexp_replace_digits", rule="REGEXP_REPLACE", expr="REGEXP_REPLACE('a1b2', '[0-9]', '')"),
    dict(id="substring", rule="SUBSTRING", expr="SUBSTRING('abcdef', 2, 3)"),
    dict(id="length_unicode", rule="LENGTH of a multibyte string", expr="LENGTH('héllo')"),
    dict(id="trim_default", rule="TRIM", expr="TRIM('  ab  ')"),
    dict(id="upper_unicode", rule="UPPER of a multibyte string", expr="UPPER('straße')"),
    dict(id="null_ordering_min", rule="MIN ignores NULL", expr="MIN(v)",
         from_clause="(SELECT CAST(NULL AS INT) AS v UNION ALL SELECT 2) AS t"),
    dict(id="count_star_vs_column", rule="COUNT(*) versus COUNT(column)",
         expr="CAST(COUNT(*) AS TEXT) || ',' || CAST(COUNT(v) AS TEXT)",
         from_clause="(SELECT CAST(NULL AS INT) AS v UNION ALL SELECT 2) AS t"),
    # --- follow-ups from the first real-warehouse run -----------------------
    dict(id="lower_unicode", rule="LOWER of a multibyte string", expr="LOWER('STRASSE')"),
    dict(id="lower_sharp_s", rule="LOWER of the capital sharp S", expr="LOWER('STRA\u1e9eE')"),
    dict(id="md5_of_double_cast", rule="MD5 over a DOUBLE cast to text",
         expr="MD5(CAST(CAST(1.0 AS DOUBLE) AS TEXT))"),
    dict(id="md5_of_int_cast", rule="MD5 over an INT cast to text",
         expr="MD5(CAST(CAST(12345 AS INT) AS TEXT))"),
    dict(id="md5_of_decimal_cast", rule="MD5 over a DECIMAL cast to text",
         expr="MD5(CAST(CAST(1.50 AS DECIMAL(10,2)) AS TEXT))"),
    dict(id="md5_of_timestamp_cast", rule="MD5 over a TIMESTAMP cast to text",
         expr="MD5(CAST(CAST('2024-03-05 10:11:12' AS TIMESTAMP) AS TEXT))"),
    dict(id="pipe_double_text", rule="|| over a DOUBLE cast to text",
         expr="'x=' || CAST(CAST(1.0 AS DOUBLE) AS TEXT)"),
    dict(id="ratio_rounded_3", rule="ROUND of a ratio to 3 places",
         expr="ROUND(CAST(CAST(2 AS DOUBLE) / CAST(3 AS DOUBLE) AS DOUBLE), 3)"),
    dict(id="float_equality_text", rule="float sum rendered as text",
         expr="CAST(CAST(0.1 AS DOUBLE) + CAST(0.2 AS DOUBLE) AS TEXT)"),
    # --- the rest of the admitted surface ----------------------------------
    # The first three rounds measured 38 of the 99 admitted node types. These
    # close the gap, weighted towards what the corpus uses most: window
    # functions, QUALIFY, COUNT(DISTINCT) and set operations.
    # --- boolean logic and comparison ---------------------------------------
    dict(id="bool_logic_and_or_not", rule="AND, OR and NOT over comparisons",
         expr="CAST(((1 < 2) AND NOT (3 >= 4)) OR (1 <> 1) AS TEXT)"),
    dict(id="compare_gt_lte", rule="> and <= over integers",
         expr="CAST((2 > 1) AS TEXT) || ',' || CAST((2 <= 2) AS TEXT)"),
    dict(id="between_inclusive", rule="BETWEEN includes both bounds",
         expr="CAST((2 BETWEEN 2 AND 3) AS TEXT)"),
    dict(id="in_list_with_null", rule="IN over a list containing NULL",
         expr="COALESCE(CAST((1 IN (2, NULL)) AS TEXT), 'NULL')"),
    dict(id="is_null_predicate", rule="IS NULL",
         expr="CAST((CAST(NULL AS INT) IS NULL) AS TEXT)"),
    dict(id="like_single_char", rule="LIKE with a single-character wildcard",
         expr="CAST(('abc' LIKE 'a_c') AS TEXT)"),
    dict(id="text_ordering_case", rule="case ordering of a text comparison",
         expr="CAST(('a' < 'B') AS TEXT)"),
    dict(id="trailing_space_equality", rule="trailing space in text equality",
         expr="CAST(('a' = 'a ') AS TEXT)"),
    # --- conditional --------------------------------------------------------
    dict(id="boolean_literal_text", rule="TRUE and FALSE literals",
         expr="CAST((TRUE OR FALSE) AS TEXT)"),
    dict(id="case_when_else", rule="CASE WHEN with ELSE",
         expr="CASE WHEN 1 = 2 THEN 'x' WHEN 2 = 2 THEN 'y' ELSE 'z' END"),
    dict(id="if_function", rule="IF(condition, a, b)", expr="IF(1 = 1, 'y', 'n')"),
    # --- arithmetic ---------------------------------------------------------
    dict(id="add_sub_mul", rule="+, - and * precedence",
         expr="CAST((2 + 3 - 1) * 2 AS TEXT)"),
    dict(id="negate_literal", rule="unary minus", expr="CAST(-(3) AS TEXT)"),
    dict(id="mod_negative", rule="MOD with a negative dividend", expr="MOD(-7, 3)"),
    dict(id="pow_fractional", rule="POW with a fractional exponent", expr="POW(2, 0.5)"),
    dict(id="ceil_floor_negative", rule="CEIL and FLOOR of a negative",
         expr="CAST(CEIL(-1.5) AS TEXT) || ',' || CAST(FLOOR(-1.5) AS TEXT)"),
    # --- aggregation and grouping -------------------------------------------
    dict(id="max_text_case", rule="MAX over mixed-case text", expr="MAX(v)",
         from_clause="(SELECT 'a' AS v UNION ALL SELECT 'B') AS t"),
    dict(id="count_distinct_nulls", rule="COUNT(DISTINCT) ignores NULL",
         expr="COUNT(DISTINCT v)",
         from_clause="(SELECT 1 AS v UNION ALL SELECT 1 UNION ALL SELECT CAST(NULL AS INT)) AS t"),
    dict(id="count_distinct_case", rule="COUNT(DISTINCT) over mixed-case text",
         expr="COUNT(DISTINCT v)",
         from_clause="(SELECT 'a' AS v UNION ALL SELECT 'A') AS t"),
    dict(id="where_filters_null", rule="WHERE drops a NULL comparison",
         expr="COUNT(*)",
         from_clause="(SELECT v FROM (SELECT CAST(NULL AS INT) AS v UNION ALL SELECT 2) AS s WHERE v > 1) AS t"),
    dict(id="group_having_count", rule="GROUP BY with HAVING", expr="MIN(c)",
         from_clause="(SELECT k, COUNT(*) AS c FROM (SELECT 1 AS k UNION ALL SELECT 1 UNION ALL SELECT 2) AS s GROUP BY k HAVING COUNT(*) > 1) AS t"),
    dict(id="filter_clause_count", rule="aggregate FILTER (WHERE ...)",
         expr="COUNT(*) FILTER (WHERE v > 1)",
         from_clause="(SELECT 1 AS v UNION ALL SELECT 2) AS t"),
    dict(id="sum_over_no_rows", rule="SUM over an empty set",
         expr="COALESCE(CAST(SUM(v) AS TEXT), 'NULL')",
         from_clause="(SELECT v FROM (SELECT 1 AS v) AS s WHERE v > 5) AS t"),
    # --- set operations -----------------------------------------------------
    dict(id="except_dedups", rule="EXCEPT removes duplicates", expr="COUNT(*)",
         from_clause="(SELECT 1 AS v UNION ALL SELECT 1 EXCEPT SELECT 2) AS t"),
    dict(id="intersect_dedups", rule="INTERSECT removes duplicates", expr="COUNT(*)",
         from_clause="(SELECT 1 AS v UNION ALL SELECT 1 INTERSECT SELECT 1) AS t"),
    dict(id="union_dedups_null", rule="UNION treats NULL as equal for dedup",
         expr="COUNT(*)",
         from_clause="(SELECT CAST(NULL AS INT) AS v UNION SELECT CAST(NULL AS INT)) AS t"),
    # --- joins --------------------------------------------------------------
    dict(id="left_join_unmatched", rule="LEFT JOIN yields NULL when nothing matches",
         expr="COALESCE(CAST(MIN(w) AS TEXT), 'NULL')",
         from_clause="(SELECT l.v AS v, r.w AS w FROM (SELECT 1 AS v) AS l LEFT JOIN (SELECT 2 AS w) AS r ON l.v = r.w) AS t"),
    dict(id="inner_join_null_key", rule="INNER JOIN does not match NULL keys",
         expr="COUNT(*)",
         from_clause="(SELECT l.v AS v FROM (SELECT CAST(NULL AS INT) AS v) AS l JOIN (SELECT CAST(NULL AS INT) AS w) AS r ON l.v = r.w) AS t"),
    # --- window functions ---------------------------------------------------
    dict(id="row_number_null_rank", rule="ROW_NUMBER: the rank given to a NULL key",
         expr="MIN(CASE WHEN v IS NULL THEN rn END)",
         from_clause="(SELECT v, ROW_NUMBER() OVER (ORDER BY v) AS rn FROM (SELECT CAST(NULL AS INT) AS v UNION ALL SELECT 2) AS s) AS t"),
    dict(id="row_number_desc_null_rank", rule="ROW_NUMBER descending: the rank of a NULL key",
         expr="MIN(CASE WHEN v IS NULL THEN rn END)",
         from_clause="(SELECT v, ROW_NUMBER() OVER (ORDER BY v DESC) AS rn FROM (SELECT CAST(NULL AS INT) AS v UNION ALL SELECT 2) AS s) AS t"),
    dict(id="rank_versus_dense_rank", rule="RANK and DENSE_RANK over ties",
         expr="CAST(MAX(r) AS TEXT) || ',' || CAST(MAX(dr) AS TEXT)",
         from_clause="(SELECT RANK() OVER (ORDER BY v) AS r, DENSE_RANK() OVER (ORDER BY v) AS dr FROM (SELECT 1 AS v UNION ALL SELECT 1 UNION ALL SELECT 2) AS s) AS t"),
    dict(id="lag_lead_edges", rule="LAG and LEAD at the partition edges",
         expr="COALESCE(CAST(MIN(lg) AS TEXT), 'NULL') || ',' || COALESCE(CAST(MAX(ld) AS TEXT), 'NULL')",
         from_clause="(SELECT LAG(v) OVER (ORDER BY v) AS lg, LEAD(v) OVER (ORDER BY v) AS ld FROM (SELECT 1 AS v UNION ALL SELECT 2) AS s) AS t"),
    dict(id="last_value_default_frame", rule="LAST_VALUE default window frame",
         expr="CAST(MIN(lv) AS TEXT) || ',' || CAST(MAX(lv) AS TEXT)",
         from_clause="(SELECT LAST_VALUE(v) OVER (ORDER BY v) AS lv FROM (SELECT 1 AS v UNION ALL SELECT 2) AS s) AS t"),
    dict(id="window_sum_default_frame", rule="SUM OVER (ORDER BY) default frame with peers",
         expr="CAST(MIN(s1) AS TEXT) || ',' || CAST(MAX(s1) AS TEXT)",
         from_clause="(SELECT SUM(v) OVER (ORDER BY k) AS s1 FROM (SELECT 1 AS k, 1 AS v UNION ALL SELECT 1, 2 UNION ALL SELECT 2, 4) AS s) AS t"),
    dict(id="window_rows_frame_explicit", rule="explicit ROWS window frame",
         expr="CAST(MAX(s1) AS TEXT)",
         from_clause="(SELECT SUM(v) OVER (ORDER BY k ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW) AS s1 FROM (SELECT 1 AS k, 1 AS v UNION ALL SELECT 2, 2) AS s) AS t"),
    dict(id="qualify_row_number", rule="QUALIFY over ROW_NUMBER", expr="COUNT(*)",
         from_clause="(SELECT v FROM (SELECT 1 AS v UNION ALL SELECT 2) AS s QUALIFY ROW_NUMBER() OVER (ORDER BY v) = 1) AS t"),
    dict(id="count_distinct_over", rule="COUNT(DISTINCT) as a window aggregate",
         expr="MAX(c)",
         from_clause="(SELECT COUNT(DISTINCT v) OVER () AS c FROM (SELECT 1 AS v UNION ALL SELECT 1) AS s) AS t"),
    # --- ordering, limit and offset -----------------------------------------
    dict(id="order_limit_offset", rule="ORDER BY with LIMIT and OFFSET", expr="MIN(v)",
         from_clause="(SELECT v FROM (SELECT 1 AS v UNION ALL SELECT 2 UNION ALL SELECT 3) AS s ORDER BY v LIMIT 1 OFFSET 1) AS t"),
    dict(id="order_nulls_explicit", rule="ORDER BY with an explicit NULLS LAST",
         expr="COALESCE(CAST(MIN(v) AS TEXT), 'NULL')",
         from_clause="(SELECT v FROM (SELECT CAST(NULL AS INT) AS v UNION ALL SELECT 2) AS s ORDER BY v NULLS LAST LIMIT 1) AS t"),
    # --- common table expressions -------------------------------------------
    dict(id="cte_reference", rule="WITH ... SELECT", expr="MIN(v)",
         from_clause="(WITH c AS (SELECT 1 AS v) SELECT v FROM c) AS t"),
    # --- temporal -----------------------------------------------------------
    dict(id="dateadd_day", rule="DATEADD across a month boundary",
         expr="CAST(DATEADD('day', 2, CAST('2024-02-28' AS DATE)) AS TEXT)"),
    dict(id="to_char_month", rule="TO_CHAR with a year-month mask",
         expr="TO_CHAR(CAST('2024-03-05 10:11:12' AS TIMESTAMP), 'YYYY-MM')"),
    dict(id="time_to_str_month", rule="STRFTIME-style timestamp formatting",
         expr="STRFTIME(CAST('2024-03-05 10:11:12' AS TIMESTAMP), '%Y-%m')"),
    # --- casts and text functions -------------------------------------------
    dict(id="try_cast_invalid", rule="TRY_CAST of non-numeric text",
         expr="COALESCE(CAST(TRY_CAST('x' AS INT) AS TEXT), 'NULL')"),
    dict(id="regexp_like_digits", rule="regular-expression matching predicate",
         expr="CAST(REGEXP_MATCHES('a1', '[0-9]') AS TEXT)"),
    dict(id="split_part_middle", rule="SPLIT_PART of the middle field",
         expr="SPLIT_PART('a,b,c', ',', 2)"),
    dict(id="bool_and_or_agg", rule="BOOL_AND and BOOL_OR",
         expr="CAST(BOOL_AND(v) AS TEXT) || ',' || CAST(BOOL_OR(v) AS TEXT)",
         from_clause="(SELECT CAST(1 AS BOOLEAN) AS v UNION ALL SELECT CAST(0 AS BOOLEAN)) AS t"),
    # --- JSON navigation: one idiom per destination -------------------------
    dict(id="json_extract_scalar", rule="JSON scalar extraction",
         expr="""CAST('{"a":{"b":"x"}}' AS JSON) ->> '$.a.b'""",
         per_destination={
             "snowflake": """PARSE_JSON('{"a":{"b":"x"}}'):a:b::string""",
             "databricks": """GET_JSON_OBJECT('{"a":{"b":"x"}}', '$.a.b')""",
             "redshift": """JSON_EXTRACT_PATH_TEXT('{"a":{"b":"x"}}', 'a', 'b')""",
         }),
    dict(id="json_extract_missing_key", rule="JSON extraction of a missing key",
         expr="""COALESCE(CAST('{"a":1}' AS JSON) ->> '$.zz', 'NULL')""",
         per_destination={
             "snowflake": """COALESCE(PARSE_JSON('{"a":1}'):zz::string, 'NULL')""",
             "databricks": """COALESCE(GET_JSON_OBJECT('{"a":1}', '$.zz'), 'NULL')""",
             "redshift": """COALESCE(NULLIF(JSON_EXTRACT_PATH_TEXT('{"a":1}', 'zz'), ''), 'NULL')""",
         }),
    dict(id="json_array_element", rule="JSON array subscript",
         expr="""CAST('{"a":[10,20]}' AS JSON) ->> '$.a[1]'""",
         per_destination={
             "snowflake": """PARSE_JSON('{"a":[10,20]}'):a[1]::string""",
             "databricks": """GET_JSON_OBJECT('{"a":[10,20]}', '$.a[1]')""",
             "redshift": """JSON_EXTRACT_PATH_TEXT('{"a":[10,20]}', 'a', '1')""",
         }),
]


def candidate_sql(probe: dict, destination) -> str:
    """What a solver targeting THIS destination writes for one probe.

    The expression is authored once in DuckDB spelling and transpiled with
    sqlglot, which is exactly how the canonical emitter renders reference SQL
    into a destination dialect. A probe overrides the rendering per destination
    when the idiom has no transpilation.

    Lives beside the probes, not in the collector, so the coverage test can
    render every probe without importing a warehouse driver.
    """

    import sqlglot

    name = getattr(destination, "value", destination)
    override = (probe.get("per_destination") or {}).get(name)
    if override is not None:
        expr = override
    else:
        expr = sqlglot.transpile(
            f"SELECT {probe['expr']}", read="duckdb", write=name
        )[0].removeprefix("SELECT ")
    frm = probe.get("from_clause")
    return f"SELECT {expr} AS v" + (f" FROM {frm}" if frm else "")
