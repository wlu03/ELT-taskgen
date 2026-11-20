from elt_taskgen.generation.plan_validation import detect_semantic_patterns, mart_inputs
from elt_taskgen.generation.recipe_admission import RecipeFamily, SemanticDifficultyError
from elt_taskgen.generation.mart_plan import quote
from elt_taskgen.generation.recipe_plan import PlanBuilder
from elt_taskgen.generation.recipe_registry import UPSTREAM_BASELINE
from elt_taskgen.generation.semantic_difficulty import NUMERIC
from elt_taskgen.models import ColumnType, MartColumn, MartColumnKind, MartOpKind, MartPlan, MartSpec, TaskIR

_OUTPUTS = frozenset({"metric_value", "population_mean", "above_population_mean"})


def candidates(task: TaskIR, recipe: str) -> tuple[tuple[str, str], ...]:
    return tuple(
        (mart.name, column.name)
        for mart in task.marts
        if mart.plan.template_id != recipe
        and not _OUTPUTS.intersection(key.casefold() for key in mart.key_columns)
        for column in mart.columns
        if column.kind is MartColumnKind.AGGREGATED
        and column.type in NUMERIC
        and column.name not in mart.key_columns
    )


def _plan(task: TaskIR, evidence: tuple[str, str], name: str, recipe: str, variant: str = "") -> MartSpec:
    model, measure = evidence
    upstream = task.mart(model)
    key_columns = tuple(column for column in upstream.columns if column.name in upstream.key_columns)
    metric = next(column for column in upstream.columns if column.name == measure)
    keys = ", ".join(map(quote, upstream.key_columns))
    ordering = ", ".join(f"{quote(key)} ASC NULLS LAST" for key in upstream.key_columns)
    value = f"COALESCE({quote(measure)}, 0)"
    frame = f"OVER (ORDER BY {ordering} ROWS BETWEEN UNBOUNDED PRECEDING AND UNBOUNDED FOLLOWING)"
    if variant == "use_maximum":
        benchmark = f"MAX({value}) {frame}"
    else:
        benchmark = f"SUM(CAST({value} AS DOUBLE)) {frame} / NULLIF(COUNT(*) {frame}, 0)"
    builder = PlanBuilder(task)
    builder.taken.update(mart.name for mart in task.marts if mart.name != name)
    baseline = builder.add(
        MartOpKind.WINDOW, (model,), "compared_rows",
        select=f"{keys}, {value} AS metric_value, {benchmark} AS population_mean",
        details={"order_by": ordering},
    )
    builder.project(
        baseline, f"{keys}, metric_value, population_mean, metric_value > population_mean AS above_population_mean",
        "comparison_output",
    )
    plan = MartPlan(mart=name, ops=tuple(builder.ops), template_id=recipe, notes=measure)
    plan = plan.model_copy(update={"semantic_patterns": detect_semantic_patterns(plan)})
    return MartSpec(
        name=name,
        description=f"Compare each row's {measure} from the {model} model with the mean across that model.",
        grain=f"One row per {', '.join(upstream.key_columns)} in {model}.",
        key_columns=upstream.key_columns,
        columns=(
            *(column.model_copy(update={"kind": MartColumnKind.PASSTHROUGH}) for column in key_columns),
            MartColumn(name="metric_value", type=metric.type, kind=MartColumnKind.DERIVED,
                       description=f"The row's {measure} in {model}, with NULL treated as zero."),
            MartColumn(name="population_mean", type=ColumnType.FLOAT, kind=MartColumnKind.RANKED,
                       description=f"Arithmetic mean of metric_value across all rows of {model}, each weighted equally."),
            MartColumn(name="above_population_mean", type=ColumnType.BOOLEAN, kind=MartColumnKind.DERIVED,
                       description="True exactly when metric_value is strictly greater than population_mean."),
        ),
        plan=plan,
    )


def _bind(task: TaskIR, mart: MartSpec) -> tuple[str, str]:
    inputs = mart_inputs(task, mart.plan)
    if len(inputs) == 1:
        evidence = (inputs[0], mart.plan.notes)
        if evidence in candidates(task, UPSTREAM_BASELINE) and _plan(task, evidence, mart.name, UPSTREAM_BASELINE) == mart:
            return evidence
    raise SemanticDifficultyError("Upstream baseline mart no longer matches its input model and measure")


_FAMILY = RecipeFamily(
    name="upstream baseline",
    recipes=(UPSTREAM_BASELINE,),
    candidates=candidates,
    plan=_plan,
    witness_rows=lambda *_: {},
    variants=lambda *_: ("use_maximum",),
    bind=_bind,
    witness_description="verified on existing counterfactual rows",
)

materialize_mutation = _FAMILY.materialize_mutation


def apply_upstream_baseline(task: TaskIR) -> TaskIR:
    return _FAMILY.apply(task, (UPSTREAM_BASELINE,))
