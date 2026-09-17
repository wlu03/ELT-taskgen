"""Tests for corpus/difficulty.py and corpus/selection.py.

Covers: structural scoring determinism and bounds, empirical attachment,
ACCEPTED-only enforcement, fail-closed measurement checks, family/cluster
split isolation properties, quota satisfaction on synthetic pools, and the
anchor profile/deviation reporting.
"""

from __future__ import annotations

import unittest

from elt_taskgen import demo_fixture
from elt_taskgen.corpus.difficulty import (
    structural_difficulty,
    structural_features,
    with_empirical,
)
from elt_taskgen.corpus.selection import (
    Quotas,
    SelectionResult,
    anchor_deviation_report,
    anchor_profile,
    band_of,
    select,
    split_for_family,
)
from elt_taskgen.models import (
    Backend,
    BackendAssignment,
    ColumnSpec,
    ColumnType,
    EmpiricalDifficulty,
    JoinType,
    MartColumn,
    MartOp,
    MartOpKind,
    MartPlan,
    MartSpec,
    Origin,
    PopulationName,
    PopulationSpec,
    TableSpec,
    TaskIR,
    TaskStatus,
)


def _ops(*, joins: int = 0, aggregates: int = 0, windows: int = 0, shaping: int = 0):
    ops = [MartOp(kind=MartOpKind.SOURCE, description="bring t in", tables=("t",))]
    for i in range(joins):
        ops.append(
            MartOp(
                kind=MartOpKind.JOIN,
                description=f"join {i}",
                join_type=JoinType.LEFT,
            )
        )
    for i in range(aggregates):
        ops.append(MartOp(kind=MartOpKind.AGGREGATE, description=f"agg {i}"))
    for i in range(windows):
        ops.append(MartOp(kind=MartOpKind.WINDOW, description=f"win {i}"))
    for i in range(shaping):
        ops.append(MartOp(kind=MartOpKind.FILTER, description=f"filter {i}"))
    return tuple(ops)


def make_task(
    n: int,
    *,
    family: str | None = None,
    cluster: str | None = None,
    status: TaskStatus = TaskStatus.ACCEPTED,
    origin: Origin = Origin.SYNTHETIC,
    backend: Backend = Backend.POSTGRES,
    heavy: bool = False,
) -> TaskIR:
    """Minimal valid TaskIR. heavy=True lands in the 'medium' band, else 'easy'."""
    tid = f"synthetic__t{n:04d}"
    fam = family or f"pool__fam{n:04d}"
    plan = MartPlan(
        mart="m",
        ops=_ops(joins=6, aggregates=6, windows=3, shaping=10) if heavy else _ops(),
    )
    return TaskIR(
        task_id=tid,
        family_id=fam,
        cluster_id=cluster or f"cluster{n:04d}",
        origin=origin,
        license="MIT",
        tables=(
            TableSpec(
                name="t",
                columns=(ColumnSpec(name="id", type=ColumnType.INTEGER),),
                primary_key=("id",),
            ),
        ),
        backends=(BackendAssignment(table="t", backend=backend),),
        marts=(
            MartSpec(
                name="m",
                grain="one row per id",
                key_columns=("id",),
                columns=(
                    MartColumn(name="id", type=ColumnType.INTEGER, description="key"),
                ),
                plan=plan,
            ),
        ),
        status=status,
    )


def measure(tasks: list[TaskIR]):
    return {t.task_id: structural_difficulty(t) for t in tasks}


def accepted(tasks: list[TaskIR]):
    """Both required unit batteries represented at the selection boundary."""
    return {t.task_id: frozenset({"extract_load", "transform"}) for t in tasks}


class DifficultyTests(unittest.TestCase):
    def test_structural_demo_task(self) -> None:
        task = demo_fixture.demo_task()
        m = structural_difficulty(task)
        self.assertEqual(m.task_id, task.task_id)
        self.assertEqual(m.task_content_hash, task.content_hash())
        self.assertIsNone(m.empirical)
        self.assertTrue(0.0 <= m.load_score <= 1.0)
        self.assertTrue(0.0 <= m.transform_score <= 1.0)
        self.assertGreater(m.transform_score, 0.0)
        self.assertGreater(m.load_score, 0.0)
        # demo has 3 tables across 3 backends and a plan with joins+aggregates
        self.assertEqual(m.structural["table_count"], 3.0)
        self.assertEqual(m.structural["backend_count"], 3.0)
        self.assertGreaterEqual(m.structural["join_count"], 1.0)
        self.assertGreaterEqual(m.structural["aggregate_count"], 1.0)
        self.assertEqual(m.structural["mart_count"], 1.0)
        self.assertEqual(m.structural["active_source_table_count"], 3.0)
        self.assertEqual(m.structural["active_lineage_ratio"], 1.0)
        self.assertEqual(m.structural["active_lineage_compiled"], 1.0)
        self.assertGreater(m.structural["primary_row_count"], 0.0)
        self.assertGreater(m.structural["stress_row_count"], 0.0)
        self.assertGreaterEqual(m.structural["semantic_interaction_count"], 1.0)
        self.assertEqual(m.structural["plan_signature_count"], 1.0)

    def test_structural_deterministic(self) -> None:
        task = demo_fixture.demo_task()
        self.assertEqual(structural_difficulty(task), structural_difficulty(task))
        self.assertEqual(structural_features(task), structural_features(task))

    def test_scores_saturate_within_bounds(self) -> None:
        heavy = make_task(1, heavy=True)
        m = structural_difficulty(heavy)
        self.assertLessEqual(m.transform_score, 1.0)
        self.assertLessEqual(m.load_score, 1.0)
        # heavier plan strictly harder than the trivial one
        easy = structural_difficulty(make_task(2))
        self.assertGreater(m.transform_score, easy.transform_score)

    def test_primary_row_volume_contributes_log_scaled_load_difficulty(self) -> None:
        base = make_task(1)
        small = base.model_copy(
            update={
                "populations": (
                    PopulationSpec(
                        name=PopulationName.PRIMARY, seed=1, scale={"t": 100}
                    ),
                )
            }
        )
        large = base.model_copy(
            update={
                "populations": (
                    PopulationSpec(
                        name=PopulationName.PRIMARY,
                        seed=1,
                        scale={"t": 1_000_000},
                    ),
                )
            }
        )
        small_measurement = structural_difficulty(small)
        large_measurement = structural_difficulty(large)
        self.assertGreater(large_measurement.load_score, small_measurement.load_score)
        self.assertEqual(
            large_measurement.structural["synthetic_primary_row_count"],
            1_000_000.0,
        )

    def test_combined_weighting(self) -> None:
        m = structural_difficulty(make_task(1, heavy=True))
        expected = 0.3 * m.load_score + 0.7 * m.transform_score
        self.assertAlmostEqual(m.combined_score(), expected)

    def test_with_empirical(self) -> None:
        m = structural_difficulty(make_task(1))
        e = EmpiricalDifficulty(
            solver_config="baseline-v1",
            n_attempts=8,
            success_rate=0.25,
            stage1_failure_rate=0.5,
            stage2_failure_rate=0.5,
        )
        m2 = with_empirical(m, e)
        self.assertIsNone(m.empirical)  # original untouched (frozen)
        self.assertEqual(m2.empirical, e)
        self.assertEqual(m2.task_content_hash, m.task_content_hash)
        self.assertEqual(m2.load_score, m.load_score)


class SelectionEligibilityTests(unittest.TestCase):
    def test_release_policy_refuses_structural_only_difficulty(self) -> None:
        task = make_task(1)
        result = select(
            [task],
            measure([task]),
            [],
            Quotas(size=1),
            accepted([task]),
            require_empirical=True,
        )
        self.assertEqual(result.train, ())
        self.assertEqual(result.val, ())
        self.assertTrue(result.empirical_required)
        self.assertIn("no empirical solver evidence", result.rejected[task.task_id])

    def test_accepted_only_enforced(self) -> None:
        ok = make_task(1)
        draft = make_task(2, status=TaskStatus.DRAFT)
        rejected_task = make_task(3, status=TaskStatus.REJECTED)
        pool = [ok, draft, rejected_task]
        res = select(pool, measure(pool), [], Quotas(size=10), accepted(pool))
        self.assertEqual(set(res.train) | set(res.val), {ok.task_id})
        self.assertIn(draft.task_id, res.rejected)
        self.assertIn(rejected_task.task_id, res.rejected)
        self.assertIn("not accepted", res.rejected[draft.task_id])
        self.assertEqual(
            res.task_content_hashes,
            {ok.task_id: ok.content_hash()},
        )
        self.assertEqual(set(res.difficulty_measurements), {ok.task_id})
        self.assertEqual(len(res.difficulty_measurements[ok.task_id]), 64)

    def test_missing_measurement_fails_closed(self) -> None:
        t = make_task(1)
        res = select([t], {}, [], Quotas(size=5), accepted([t]))
        self.assertEqual(res.train, ())
        self.assertEqual(res.val, ())
        self.assertIn("no difficulty measurement", res.rejected[t.task_id])

    def test_stale_measurement_fails_closed(self) -> None:
        t = make_task(1)
        stale = structural_difficulty(t).model_copy(
            update={"task_content_hash": "0" * 64}
        )
        res = select([t], {t.task_id: stale}, [], Quotas(size=5), accepted([t]))
        self.assertIn("stale", res.rejected[t.task_id])

    def test_measurement_for_another_task_fails_closed(self) -> None:
        task = make_task(1)
        foreign = structural_difficulty(task).model_copy(
            update={"task_id": "synthetic__another_task"}
        )
        result = select(
            [task], {task.task_id: foreign}, [], Quotas(size=1), accepted([task])
        )
        self.assertEqual(result.train, ())
        self.assertEqual(result.val, ())
        self.assertIn("another task", result.rejected[task.task_id])

    def test_anchor_origin_never_selected(self) -> None:
        anchor = make_task(
            1, family="eltbench__flight", origin=Origin.ELTBENCH_ANCHOR
        )
        res = select(
            [anchor], measure([anchor]), [], Quotas(size=5), accepted([anchor])
        )
        self.assertIn("measurement-only", res.rejected[anchor.task_id])


class SplitIsolationTests(unittest.TestCase):
    def test_family_uniqueness_and_split_isolation(self) -> None:
        # 20 families, two tasks in some families
        pool = [make_task(i) for i in range(20)]
        pool += [
            make_task(100 + i, family=f"pool__fam{i:04d}") for i in range(5)
        ]  # duplicates of families 0-4
        res = select(
            pool, measure(pool), [], Quotas(size=50, val_fraction=0.5), accepted(pool)
        )
        by_id = {t.task_id: t for t in pool}
        train_families = {by_id[t].family_id for t in res.train}
        val_families = {by_id[t].family_id for t in res.val}
        # property: never two tasks from one family across train ∪ val
        self.assertEqual(train_families & val_families, set())
        selected = list(res.train) + list(res.val)
        selected_families = [by_id[t].family_id for t in selected]
        self.assertEqual(len(selected_families), len(set(selected_families)))
        # the duplicate-family extras were rejected with a family reason
        for i in range(5):
            dup_id = f"synthetic__t{100 + i:04d}"
            self.assertIn("family", res.rejected[dup_id])

    def test_cluster_never_straddles_splits(self) -> None:
        a = make_task(1, cluster="shared")
        b = make_task(2, cluster="shared")
        pool = [a, b]
        res = select(
            pool, measure(pool), [], Quotas(size=10, val_fraction=0.5), accepted(pool)
        )
        selected = set(res.train) | set(res.val)
        self.assertEqual(len(selected & {a.task_id, b.task_id}), 1)
        self.assertIn("cluster", res.rejected[b.task_id])

    def test_split_hash_stable_by_family(self) -> None:
        # pure function of family_id + fraction
        for fam in ("pool__a", "pool__b", "pool__c"):
            self.assertEqual(
                split_for_family(fam, 0.5), split_for_family(fam, 0.5)
            )
        # a family's side does not move when the pool changes
        base = [make_task(i) for i in range(10)]
        grown = base + [make_task(i) for i in range(10, 30)]
        r1 = select(
            base, measure(base), [], Quotas(size=50, val_fraction=0.5), accepted(base)
        )
        r2 = select(
            grown, measure(grown), [], Quotas(size=50, val_fraction=0.5), accepted(grown)
        )
        val1 = set(r1.val)
        val2 = set(r2.val)
        for tid in set(r1.train) | val1:
            self.assertEqual(tid in val1, tid in val2)

    def test_selection_deterministic(self) -> None:
        pool = [make_task(i, heavy=(i % 2 == 0)) for i in range(12)]
        ms = measure(pool)
        q = Quotas(size=8, val_fraction=0.25)
        self.assertEqual(
            select(pool, ms, [], q, accepted(pool)),
            select(list(reversed(pool)), ms, [], q, accepted(pool)),
        )


class QuotaTests(unittest.TestCase):
    def test_band_assignment(self) -> None:
        easy = structural_difficulty(make_task(1)).combined_score()
        medium = structural_difficulty(make_task(2, heavy=True)).combined_score()
        self.assertEqual(band_of(easy), "easy")
        self.assertEqual(band_of(medium), "medium")

    def test_difficulty_band_quota_satisfied(self) -> None:
        pool = [make_task(i) for i in range(4)]  # easy
        pool += [make_task(10 + i, heavy=True) for i in range(4)]  # medium
        ms = measure(pool)
        q = Quotas(
            size=4,
            difficulty_bands={"easy": 0.5, "medium": 0.5},
            val_fraction=0.0,
        )
        res = select(pool, ms, [], q, accepted(pool))
        selected = list(res.train) + list(res.val)
        self.assertEqual(len(selected), 4)
        bands = [band_of(ms[t].combined_score()) for t in selected]
        self.assertEqual(bands.count("easy"), 2)
        self.assertEqual(bands.count("medium"), 2)

    def test_size_cap_and_overflow_reason(self) -> None:
        pool = [make_task(i) for i in range(6)]
        res = select(
            pool, measure(pool), [], Quotas(size=3, val_fraction=0.0), accepted(pool)
        )
        selected = list(res.train) + list(res.val)
        self.assertEqual(len(selected), 3)
        overflow = [t.task_id for t in pool if t.task_id not in selected]
        self.assertEqual(len(overflow), 3)
        for tid in overflow:
            self.assertIn("quota/size", res.rejected[tid])

    def test_origin_quota_steers_picks(self) -> None:
        dbt = [make_task(i, family=f"dbt__f{i}", origin=Origin.DBT) for i in range(3)]
        syn = [make_task(10 + i) for i in range(3)]
        pool = dbt + syn
        ms = measure(pool)
        q = Quotas(
            size=2,
            origin_mix={Origin.DBT: 1.0},
            val_fraction=0.0,
        )
        res = select(pool, ms, [], q, accepted(pool))
        by_id = {t.task_id: t for t in pool}
        selected_origins = [
            by_id[t].origin for t in list(res.train) + list(res.val)
        ]
        self.assertEqual(selected_origins, [Origin.DBT, Origin.DBT])

    def test_backend_quota_steers_picks(self) -> None:
        pg = [make_task(i) for i in range(3)]
        rest = [make_task(10 + i, backend=Backend.REST) for i in range(3)]
        pool = pg + rest
        ms = measure(pool)
        q = Quotas(size=2, backend_mix={Backend.REST: 1.0}, val_fraction=0.0)
        res = select(pool, ms, [], q, accepted(pool))
        by_id = {t.task_id: t for t in pool}
        for tid in list(res.train) + list(res.val):
            self.assertEqual(by_id[tid].backends[0].backend, Backend.REST)


class AnchorProfileTests(unittest.TestCase):
    def test_profile_and_deviation(self) -> None:
        anchors = [demo_fixture.demo_task()]
        profile = anchor_profile(anchors)
        self.assertIn("load_score", profile)
        self.assertIn("transform_score", profile)
        self.assertIn("combined_score", profile)
        self.assertIn("join_count", profile)
        q25, q50, q75 = profile["table_count"]
        self.assertEqual((q25, q50, q75), (3.0, 3.0, 3.0))

        pool = [make_task(i) for i in range(4)]
        ms = measure(pool)
        report = anchor_deviation_report([t.task_id for t in pool], ms, profile)
        # synthetic single-table tasks are lighter than the demo anchor
        self.assertEqual(report["table_count"], 1.0 - 3.0)
        self.assertLess(report["transform_score"], 0.0)

    def test_profile_fails_closed_on_empty(self) -> None:
        with self.assertRaises(ValueError):
            anchor_profile([])
        with self.assertRaises(ValueError):
            anchor_deviation_report([], {}, {"x": (0.0, 0.0, 0.0)})


class SelectionResultShapeTests(unittest.TestCase):
    def test_result_model_frozen(self) -> None:
        res = SelectionResult(
            train=("a",),
            val=(),
            rejected={},
            variants={"a": ("extract_load", "transform")},
        )
        with self.assertRaises(Exception):
            res.train = ("b",)  # type: ignore[misc]


if __name__ == "__main__":
    unittest.main()
