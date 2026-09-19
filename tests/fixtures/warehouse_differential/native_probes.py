"""Native differential probes: SQL written by hand in each destination's dialect.

probes.py authors every expression once in DuckDB spelling and transpiles it to
the destination, so the rewrite it measures starts from sqlglot's own output.
These probes start from the spelling a solver writes for its destination, and
target the compatibility questions a transpiled probe cannot reach: bare
DECIMAL casts, text and hash casts whose operand type is hidden behind an
alias, and LAST_VALUE with no window frame. Every probe is read-only and
literal-only, like probes.py.
"""

DESTINATIONS = ("snowflake", "databricks", "redshift")
TEXT = {"snowflake": "VARCHAR", "databricks": "STRING", "redshift": "VARCHAR(MAX)"}
DOUBLE = {"snowflake": "DOUBLE", "databricks": "DOUBLE", "redshift": "DOUBLE PRECISION"}

#: Two partitions; the first has a NULL ordering key, so each destination's
#: NULL placement and default frame both show in the folded result.
_WINDOW_ROWS = (
    "(SELECT 1 AS k, 1 AS o, 10 AS x UNION ALL SELECT 1, 2, 20 UNION ALL SELECT 1, 3, 30 "
    "UNION ALL SELECT 1, CAST(NULL AS INT), 40 UNION ALL SELECT 2, 5, 50 "
    "UNION ALL SELECT 2, 6, 60)"
)


def _each(template: str) -> dict[str, str]:
    return {d: template.format(text=TEXT[d], double=DOUBLE[d]) for d in DESTINATIONS}


NATIVE_PROBES = [
    # --- bare DECIMAL: each destination has its own default precision/scale --
    dict(id="bare_decimal_scale", rule="bare DECIMAL cast of 1.2345",
         native=_each("SELECT CAST(1.2345 AS DECIMAL) AS v")),
    dict(id="bare_decimal_half", rule="bare DECIMAL cast of -1.5",
         native=_each("SELECT CAST(-1.5 AS DECIMAL) AS v")),
    dict(id="bare_decimal_overflow", rule="bare DECIMAL cast of an 11-digit value",
         native=_each("SELECT CAST(12345678901.5 AS DECIMAL) AS v")),
    dict(id="explicit_decimal_default", rule="explicit DECIMAL(18,0) cast of 1.2345",
         native=_each("SELECT CAST(1.2345 AS DECIMAL(18, 0)) AS v")),
    # --- text and hash casts whose operand type is behind an alias ----------
    dict(id="cte_double_text", rule="DOUBLE cast to text through a CTE alias",
         native=_each("WITH s AS (SELECT CAST(1.0 AS {double}) AS x) SELECT CAST(x AS {text}) AS v FROM s")),
    dict(id="subquery_double_md5", rule="MD5 over a DOUBLE cast to text through a subquery alias",
         native=_each("SELECT MD5(CAST(x AS {text})) AS v FROM "
                      "(SELECT CAST(0.1 AS {double}) + CAST(0.2 AS {double}) AS x) AS s")),
    dict(id="cte_double_text_shorthand", rule="DOUBLE cast to text with :: through a CTE alias",
         native={
             "snowflake": "WITH s AS (SELECT 1.0::DOUBLE AS x) SELECT x::VARCHAR AS v FROM s",
             "databricks": "WITH s AS (SELECT 1.0::DOUBLE AS x) SELECT x::STRING AS v FROM s",
             "redshift": "WITH s AS (SELECT 1.0::DOUBLE PRECISION AS x) SELECT x::VARCHAR AS v FROM s",
         }),
    dict(id="cte_bool_text", rule="a boolean cast to text through a CTE alias",
         native=_each("WITH s AS (SELECT (1 < 2) AS f) SELECT CAST(f AS {text}) AS v FROM s")),
    # --- LAST_VALUE with ORDER BY and no frame ---------------------------------
    dict(id="last_value_no_frame", rule="LAST_VALUE with ORDER BY and no frame, NULL key present",
         native=_each(
             "SELECT CAST(SUM(lv) AS {text}) || ',' || CAST(MIN(lv) AS {text}) || ',' "
             "|| CAST(MAX(lv) AS {text}) AS v FROM (SELECT LAST_VALUE(x) OVER "
             "(PARTITION BY k ORDER BY o) AS lv FROM " + _WINDOW_ROWS + " AS d) AS w"
         )),
]
