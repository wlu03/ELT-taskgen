"""The parity battery: does the real stack agree with the DuckDB proxy?

Four components, all exercised offline (Phase 5 decision: no cloud call, no
Docker invocation, no real `runc`/`runsc`, no live model call; every observation
is injected):

  1. differential replay through `terraform validate -json` and the
     `runtime verify-*` verbs, with both runners injected as doubles;
  2. proxy-rule mutation, proving the battery kills every weakened comparator
     and that an emulator mutation FAILS the acceptance lane;
  3. metamorphic TLP and NoREC checks over the DuckDB comparator on the
     committed five-backend benchmark fixture;
  4. sampled real-warehouse replay, which is (1) with the destination runner
     bound to a real destination -- the owner's, never run here.

Plus the statistics the claim rests on: faults leave the denominator (C7), the
claim names its population set, and 0 disagreements in 20 is a one-sided 85 %
Wilson upper bound of 0.051.
"""

from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path

import duckdb

try:  # the sealed-attestation double, reused rather than rebuilt
    import test_attestation_gate as attestation_fixture
except ModuleNotFoundError:  # package-style invocation
    from tests import test_attestation_gate as attestation_fixture

from elt_taskgen import cli
from elt_taskgen.models import PopulationName, canonical_json
from elt_taskgen.review.metrology import wilson_interval
from elt_taskgen.runtime import parity
from elt_taskgen.semantic import load_semantic_package
from elt_taskgen.verification import strict_diagnostic, upstream_eval

REPO_ROOT = Path(__file__).resolve().parents[1]
FIXTURE_RELEASE = REPO_ROOT / "tests" / "fixtures" / "semantic_gate" / "release"
FIXTURE_TASK_ID = "gate__five_backend_probe"
ANSWER_KEY = FIXTURE_RELEASE / "private" / FIXTURE_TASK_ID / "answer_key"

P = PopulationName

#: Five source pools x six artifacts = 20 accepted + 10 rejected, which is
#: exactly the Table 9 floor (20 accepted, 10 rejected, 4 per pool).
SAMPLE_POOLS = ("dbt", "dlt", "schemapile", "synsql", "wikidbs")


def _load_tool():
    name = "parity_sample"
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(
        name, REPO_ROOT / "tools" / "parity_sample.py"
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def verdict(
    artifact_id: str,
    *,
    arm: str = "P2",
    pool: str = "dbt",
    destination: str = "snowflake",
    population: str = "primary",
    stage: parity.ReplayStage = parity.ReplayStage.END_TO_END,
    local: bool = True,
    real: bool | None = True,
    reward: float | None = None,
    fault_code: str = "",
) -> parity.ArtifactVerdict:
    if real is None:
        payload_reward = None
    elif reward is None:
        payload_reward = 1.0 if real else 0.0
    else:
        payload_reward = reward
    return parity.ArtifactVerdict(
        artifact_id=artifact_id,
        arm=arm,
        destination=destination,
        source_pool=pool,
        population=population,
        stage=stage,
        local_accepted=local,
        real_accepted=real,
        reward=payload_reward,
        fault_code=fault_code,
    )


def clean_sample(arm: str = "P2") -> tuple[parity.ArtifactVerdict, ...]:
    """A sample that meets every floor and shows no disagreement."""
    rows: list[parity.ArtifactVerdict] = []
    for pool in SAMPLE_POOLS:
        for index in range(4):
            rows.append(
                verdict(f"{arm}-{pool}-acc-{index}", arm=arm, pool=pool, local=True, real=True)
            )
        for index in range(2):
            rows.append(
                verdict(
                    f"{arm}-{pool}-rej-{index}",
                    arm=arm,
                    pool=pool,
                    local=False,
                    real=False,
                )
            )
    return tuple(rows)


def terraform_document(*, errors: int = 0, warnings: int = 0) -> dict:
    diagnostics = [{"severity": "error", "summary": "x"} for _ in range(errors)]
    diagnostics += [{"severity": "warning", "summary": "y"} for _ in range(warnings)]
    return {
        "format_version": "1.0",
        "valid": errors == 0,
        "error_count": errors,
        "warning_count": warnings,
        "diagnostics": diagnostics,
    }


class _FixtureMixin:
    """The COMMITTED five-backend benchmark fixture, loaded once."""

    @classmethod
    def load_fixture(cls) -> None:
        cls.package = load_semantic_package(
            FIXTURE_RELEASE, FIXTURE_TASK_ID, verify=False
        )
        cls.marts = {mart.name: mart for mart in cls.package.task.marts}
        cls.gold = {
            name: (ANSWER_KEY / "gold" / "primary" / f"{name}.csv").read_text(
                encoding="utf-8"
            )
            for name in cls.marts
        }

    @classmethod
    def open_fixture_duckdb(cls):
        """Load the frozen rendered sources, then the frozen reference marts."""
        connection = duckdb.connect(":memory:")
        strict_diagnostic.load_sources_duckdb_strict(
            cls.package.task, cls.package.source_root(P.PRIMARY), connection
        )
        for name in sorted(cls.marts):
            sql = (
                (ANSWER_KEY / "reference" / f"{name}.sql")
                .read_text(encoding="utf-8")
                .rstrip()
                .rstrip(";")
            )
            connection.execute(
                f"CREATE OR REPLACE VIEW {name} AS SELECT * FROM ({sql})"
            )
        return connection


# ---------------------------------------------------------------------------
# The rate: faults, the named population set, sizing
# ---------------------------------------------------------------------------

class ParityRateTests(unittest.TestCase):
    def test_parity_report_treats_runtime_faults_as_none_not_disagreement(self):
        """C7: an unmeasurable artifact is reward None and LEAVES the denominator.

        The failure this pins is the one that would quietly poison the claim:
        counting a warehouse timeout as "the real stack rejected it" turns
        infrastructure flake into a parity defect (and, in the other direction,
        counting it as an agreement would hide a real one).  Both are refused --
        the artifact is simply not in the denominator, and the report says how
        many were dropped and why.
        """
        rows = list(clean_sample())
        # Three of the twenty accepted artifacts could not be measured.
        for index, code in enumerate(
            ("runner_timeout", "connection_error", "runner_exit_2")
        ):
            target = rows[index]
            rows[index] = verdict(
                target.artifact_id,
                arm=target.arm,
                pool=target.source_pool,
                local=True,
                real=None,
                fault_code=code,
            )
        report = parity.parity_rate(rows)

        self.assertEqual(report.locally_accepted, 20)
        self.assertEqual(report.runtime_faults, 3)
        self.assertEqual(report.denominator, 17, "faults must leave the denominator")
        self.assertEqual(report.agreements, 17)
        self.assertEqual(report.disagreements, 0)
        self.assertEqual(report.pi_c, 0.0)
        self.assertEqual(report.quarantine_artifact_ids, ())
        self.assertEqual(
            report.fault_codes,
            {"connection_error": 1, "runner_exit_2": 1, "runner_timeout": 1},
        )
        # Every faulted record carries reward None, and nothing else does.
        for row in rows:
            if row.real_accepted is None:
                self.assertIsNone(row.reward)
                self.assertIn(row.fault_code, parity.PARITY_FAULT_CODES)
            else:
                self.assertIsNotNone(row.reward)
                self.assertEqual(row.fault_code, "")
        # The bound is the bound for n = 17, NOT for n = 20, and certainly not
        # the bound that treating the three faults as disagreements would give.
        self.assertAlmostEqual(
            report.pi_c_upper_85, wilson_interval(0, 17, 0.85)[1], places=12
        )
        self.assertNotAlmostEqual(
            report.pi_c_upper_85, wilson_interval(0, 20, 0.85)[1], places=6
        )
        self.assertLess(report.pi_c_upper_85, wilson_interval(3, 20, 0.85)[1])
        self.assertIn("real-runtime fault(s) recorded as reward None", report.claim)

    def test_a_real_rejection_is_a_disagreement_and_quarantines_the_task(self):
        rows = list(clean_sample())
        target = rows[0]
        rows[0] = verdict(
            target.artifact_id,
            arm=target.arm,
            pool=target.source_pool,
            local=True,
            real=False,
        )
        report = parity.parity_rate(rows)
        self.assertEqual(report.denominator, 20)
        self.assertEqual(report.disagreements, 1)
        self.assertEqual(report.quarantine_artifact_ids, (target.artifact_id,))
        self.assertAlmostEqual(report.pi_c, 0.05)

    def test_parity_claim_names_its_population_set(self):
        """A rate without a stated population is not a measurement.

        The report must enumerate the frame the conditional probability ranges
        over (the LOCALLY ACCEPTED artifacts), digest its membership, and repeat
        the name inside the quotable claim sentence.  Two different frames that
        happen to produce the same number must not be confusable.
        """
        rows = clean_sample()
        report = parity.parity_rate(rows)
        named = report.population_set

        self.assertTrue(named.name)
        self.assertIn(named.name, report.claim)
        self.assertIn("named population set", report.claim)
        self.assertEqual(named.arm, "P2")
        self.assertEqual(named.populations, ("primary",))
        self.assertEqual(named.destinations, ("snowflake",))
        self.assertEqual(named.source_pools, tuple(sorted(SAMPLE_POOLS)))
        self.assertEqual(named.stages, ("end_to_end",))
        # The frame is the CONDITIONING set: the 20 locally accepted artifacts,
        # not the 30 sampled ones.
        self.assertEqual(named.size, 20)
        self.assertEqual(
            named.artifact_ids,
            tuple(sorted(row.artifact_id for row in rows if row.local_accepted)),
        )
        self.assertRegex(named.frame_digest, r"^[0-9a-f]{64}$")

        # A different frame is a different digest even at the same rate.
        smaller = [row for row in rows if row.artifact_id != named.artifact_ids[0]]
        other = parity.parity_rate(smaller)
        self.assertEqual(other.pi_c, report.pi_c)
        self.assertNotEqual(other.population_set.frame_digest, named.frame_digest)
        self.assertNotEqual(other.population_set.name, named.name)

        # Serialization keeps the name attached to the number.
        payload = json.loads(canonical_json(report.model_dump(mode="json")))
        self.assertEqual(payload["population_set"]["name"], named.name)
        self.assertIn(named.name, payload["claim"])

        # And a claim over nothing is refused rather than printed as 0.0.
        with self.assertRaises(parity.ParityBatteryError):
            parity.parity_rate([])
        with self.assertRaises(parity.ParityBatteryError):
            parity.parity_rate([row for row in rows if not row.local_accepted])

    def test_one_report_covers_one_arm(self):
        rows = list(clean_sample("P2")) + list(clean_sample("P3"))
        with self.assertRaises(parity.ParityBatteryError):
            parity.parity_rate(rows)

    def test_duplicate_artifacts_are_refused(self):
        rows = list(clean_sample())
        with self.assertRaises(parity.ParityBatteryError):
            parity.parity_rate(rows + [rows[0]])

    def test_sizing_zero_disagreements_in_twenty_bounds_pi_c_at_five_percent(self):
        """Output 12 §8 sizing: 0/20 gives a one-sided 85 % UB of 0.051.

        This is the number the phase's power claim rests on, so it is pinned
        rather than recomputed by a reader: 0.051 at 85 %, 0.119 at 95 %, and a
        5 % bound at 95 % needs about 52 artifacts, not 20.
        """
        report = parity.parity_rate(clean_sample())
        self.assertEqual(report.denominator, 20)
        self.assertEqual(report.disagreements, 0)
        self.assertEqual(report.pi_c, 0.0)
        self.assertAlmostEqual(report.pi_c_upper_85, 0.051, places=3)
        self.assertAlmostEqual(report.pi_c_upper_95, 0.119, places=3)
        self.assertAlmostEqual(report.pi_c_upper_85, 0.05097201639828452, places=12)
        self.assertAlmostEqual(report.pi_c_upper_95, 0.11915783736096437, places=12)
        # The agreement bound is the complement, and it clears the 0.90 gate.
        self.assertAlmostEqual(
            report.agreement_lower_85, 1.0 - report.pi_c_upper_85, places=12
        )
        self.assertGreaterEqual(
            report.agreement_lower, parity.AGREEMENT_LOWER_BOUND_THRESHOLD
        )
        self.assertTrue(report.adopted)
        self.assertEqual(report.sizing_shortfalls, ())
        # Twenty is not enough for a 5 % bound at 95 %.
        self.assertGreater(report.pi_c_upper_95, 0.05)
        self.assertLessEqual(wilson_interval(0, 52, 0.95)[1], 0.05)

    def test_sizing_shortfalls_block_adoption_without_changing_the_rate(self):
        rows = [row for row in clean_sample() if row.source_pool != "wikidbs"]
        report = parity.parity_rate(rows)
        self.assertEqual(report.pi_c, 0.0)
        self.assertFalse(report.adopted)
        self.assertIn(
            "accepted_below_minimum:P2:16/20", report.sizing_shortfalls
        )
        self.assertIn("rejected_below_minimum:P2:8/10", report.sizing_shortfalls)

    def test_source_pool_floor_is_reported(self):
        rows = [
            row
            for row in clean_sample()
            if row.source_pool != "wikidbs" or row.artifact_id.endswith("acc-0")
        ]
        shortfalls = parity.stratification_shortfalls(rows)
        self.assertIn("source_pool_below_minimum:wikidbs:1/4", shortfalls)

    def test_mcnemar_mid_p_runs_over_the_pairs_both_arms_judged(self):
        rows = list(clean_sample())
        # Two local-accept/real-reject, one local-reject/real-accept, and one
        # fault that must not join the paired set.
        rows[0] = verdict(rows[0].artifact_id, pool=rows[0].source_pool, local=True, real=False)
        rows[1] = verdict(rows[1].artifact_id, pool=rows[1].source_pool, local=True, real=False)
        rejected = [row for row in rows if not row.local_accepted][0]
        rows[rows.index(rejected)] = verdict(
            rejected.artifact_id, pool=rejected.source_pool, local=False, real=True
        )
        rows[2] = verdict(
            rows[2].artifact_id,
            pool=rows[2].source_pool,
            local=True,
            real=None,
            fault_code="runner_timeout",
        )
        report = parity.parity_rate(rows)
        self.assertEqual(report.mcnemar_pairs, 29)
        self.assertEqual(report.mcnemar_local_only, 2)
        self.assertEqual(report.mcnemar_real_only, 1)
        self.assertAlmostEqual(
            report.mcnemar_mid_p, parity.mcnemar_mid_p(2, 1), places=12
        )

    def test_mcnemar_mid_p_matches_the_exact_binomial_construction(self):
        self.assertEqual(parity.mcnemar_mid_p(0, 0), 1.0)
        self.assertAlmostEqual(parity.mcnemar_mid_p(3, 0), 0.125, places=12)
        self.assertAlmostEqual(parity.mcnemar_mid_p(0, 3), 0.125, places=12)
        self.assertAlmostEqual(parity.mcnemar_mid_p(1, 1), 1.0, places=12)
        self.assertAlmostEqual(parity.mcnemar_mid_p(5, 0), 2 * 0.5 / 32, places=12)
        self.assertLess(parity.mcnemar_mid_p(8, 0), parity.mcnemar_mid_p(4, 0))

    def test_an_all_fault_arm_reports_no_rate_rather_than_a_perfect_one(self):
        """Zero measurable artifacts is "we do not know", not "pi_c = 0"."""
        rows = [
            verdict(
                row.artifact_id,
                pool=row.source_pool,
                local=row.local_accepted,
                real=None,
                fault_code="connection_error",
            )
            for row in clean_sample()
        ]
        report = parity.parity_rate(rows)
        self.assertEqual(report.denominator, 0)
        self.assertIsNone(report.pi_c)
        self.assertEqual(report.runtime_faults, 20)
        self.assertEqual(report.pi_c_upper_85, 1.0)
        self.assertEqual(report.agreement_lower_85, 0.0)
        self.assertFalse(report.adopted)
        self.assertIn("undefined (no measured verdict)", report.claim)
        self.assertEqual(report.mcnemar_pairs, 0)
        self.assertEqual(report.mcnemar_mid_p, 1.0)

    def test_a_fault_record_cannot_carry_a_reward(self):
        with self.assertRaises(ValueError):
            parity.ArtifactVerdict(
                artifact_id="a",
                arm="P2",
                destination="snowflake",
                source_pool="dbt",
                population="primary",
                stage=parity.ReplayStage.STAGE1,
                local_accepted=True,
                real_accepted=None,
                reward=0.0,
                fault_code="runner_timeout",
            )
        with self.assertRaises(ValueError):
            parity.ArtifactVerdict(
                artifact_id="a",
                arm="P2",
                destination="snowflake",
                source_pool="dbt",
                population="primary",
                stage=parity.ReplayStage.STAGE1,
                local_accepted=True,
                real_accepted=False,
                reward=0.0,
                fault_code="runner_timeout",
            )
        with self.assertRaises(ValueError):
            parity.ArtifactVerdict(
                artifact_id="a",
                arm="P2",
                destination="snowflake",
                source_pool="dbt",
                population="primary",
                stage=parity.ReplayStage.STAGE1,
                local_accepted=True,
                real_accepted=None,
                reward=None,
                fault_code="the warehouse said no",
            )

    def test_report_is_written_per_arm_and_is_deterministic(self):
        report = parity.parity_rate(clean_sample("P3"))
        with tempfile.TemporaryDirectory() as tmp:
            path = parity.write_parity_report(report, Path(tmp) / "reports")
            self.assertEqual(path.name, "parity_P3.json")
            payload = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(payload["arm"], "P3")
        self.assertEqual(
            payload["parity_report_schema_version"],
            parity.PARITY_REPORT_SCHEMA_VERSION,
        )
        again = parity.parity_rate(clean_sample("P3"))
        self.assertEqual(
            parity.parity_report_digest(report), parity.parity_report_digest(again)
        )

    def test_parity_report_is_sealed_and_names_the_isolation_it_measured(self):
        """A parity claim is evidence, so it is tamper-evident and attributed.

        Two cross-track properties, both about the same sentence a reader is
        allowed to quote:

        * SEALED the way every other Phase 5 record is sealed (the discipline
          `export/certification.seal_attestation` states and the sandbox
          attestation reuses): sha256 over the canonical record with the digest
          field blanked, so `write_parity_report` publishes a report an edit
          cannot survive.  Without this the one artifact of the battery would
          be the one artifact nobody could check.
        * ATTRIBUTED to the isolation it was measured under.  A claim about a
          real warehouse is a claim about a real stack, and two arms replayed
          under different container runtimes are not the same measurement, so
          the report carries the sealed `SandboxAttestation`'s digest.  The
          attestation is VERIFIED before it is named: an unsealed or edited
          record is refused rather than recorded.
        """
        record = attestation_fixture.attestation(tier="A")
        report = parity.parity_rate(clean_sample("P2"), attestation=record)
        self.assertEqual(
            report.sandbox_attestation_digest, record.attestation_digest
        )
        self.assertEqual(len(record.attestation_digest), 64)

        # Unsealed and unattested reports are distinguishable, and the seal
        # blanks its own field so both digests agree.
        self.assertEqual(report.report_digest, "")
        sealed = parity.seal_parity_report(report)
        self.assertEqual(
            sealed.report_digest, parity.parity_report_digest(report)
        )
        self.assertEqual(parity.verify_parity_report(sealed), sealed)
        with self.assertRaises(parity.ParityBatteryError):
            parity.verify_parity_report(report)
        edited = sealed.model_copy(update={"adopted": not sealed.adopted})
        with self.assertRaises(parity.ParityBatteryError):
            parity.verify_parity_report(edited)

        # Published reports are sealed on the way out.
        with tempfile.TemporaryDirectory() as tmp:
            path = parity.write_parity_report(report, Path(tmp) / "reports")
            payload = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(payload["report_digest"], sealed.report_digest)
        self.assertEqual(
            payload["sandbox_attestation_digest"], record.attestation_digest
        )
        parity.verify_parity_report(parity.ParityReport.model_validate(payload))

        # An attestation whose seal does not reproduce is refused, not named:
        # a report must never attribute itself to isolation nobody can verify.
        tampered = record.model_copy(update={"tier": "B"})
        with self.assertRaises(parity.ParityBatteryError):
            parity.parity_rate(clean_sample("P2"), attestation=tampered)

        # The offline components name no destination, so no attestation is
        # required to compute a rate at all.
        self.assertEqual(
            parity.parity_rate(clean_sample("P2")).sandbox_attestation_digest, ""
        )


# ---------------------------------------------------------------------------
# Component 1: differential replay
# ---------------------------------------------------------------------------

class TerraformValidateTests(unittest.TestCase):
    def test_terraform_validate_json_format_1_0_parsed(self):
        """`terraform validate -json` format 1.0, parsed strictly or refused.

        A parity claim built on a document the battery did not understand is
        worse than no claim, so the version is pinned exactly, the key roster is
        closed, duplicate keys are refused, and the summary counts must agree
        with the diagnostics they summarise.
        """
        clean = parity.parse_terraform_validate_json(
            json.dumps(terraform_document())
        )
        self.assertEqual(
            clean.format_version, parity.TERRAFORM_VALIDATE_FORMAT_VERSION
        )
        self.assertEqual(clean.format_version, "1.0")
        self.assertTrue(clean.valid)
        self.assertEqual(clean.error_count, 0)
        self.assertEqual(clean.diagnostic_severities, ())

        mixed = parity.parse_terraform_validate_json(
            json.dumps(terraform_document(errors=2, warnings=1)).encode("utf-8")
        )
        self.assertFalse(mixed.valid)
        self.assertEqual(mixed.error_count, 2)
        self.assertEqual(mixed.warning_count, 1)
        self.assertEqual(
            mixed.diagnostic_severities, ("error", "error", "warning")
        )

        refusals = {
            "wrong format": {**terraform_document(), "format_version": "2.0"},
            "no format": {
                key: value
                for key, value in terraform_document().items()
                if key != "format_version"
            },
            "unknown key": {**terraform_document(), "provider_schemas": {}},
            "valid contradicts errors": {
                **terraform_document(errors=1),
                "valid": True,
            },
            "count contradicts diagnostics": {
                **terraform_document(errors=1),
                "error_count": 2,
            },
            "boolean count": {**terraform_document(), "error_count": True},
            "negative count": {**terraform_document(), "error_count": -1},
            "non-boolean valid": {**terraform_document(), "valid": 1},
            "unknown severity": {
                **terraform_document(),
                "diagnostics": [{"severity": "info"}],
            },
            "diagnostics not a list": {**terraform_document(), "diagnostics": {}},
        }
        for label, document in refusals.items():
            with self.subTest(refusal=label):
                with self.assertRaises(parity.ParityBatteryError):
                    parity.parse_terraform_validate_json(json.dumps(document))
        for label, text in {
            "not json": "terraform: command not found",
            "not an object": "[]",
            "duplicate keys": '{"format_version": "1.0", "format_version": "2.0"}',
        }.items():
            with self.subTest(refusal=label):
                with self.assertRaises(parity.ParityBatteryError):
                    parity.parse_terraform_validate_json(text)
        with self.assertRaises(parity.ParityBatteryError):
            parity.parse_terraform_validate_json("x" * (5 * 1024 * 1024))

    def test_pins_are_declared_with_their_sources(self):
        self.assertEqual(parity.TERRAFORM_VALIDATE_FORMAT_VERSION, "1.0")
        self.assertEqual(parity.TERRAFORM_VERSION_PIN, "1.15.8")
        self.assertEqual(parity.PYTHON_HCL2_PIN, "7.3.1")

    def test_python_hcl2_pinned_exactly(self):
        """The installed python-hcl2 must be the version the battery pins.

        7.x and 8.x disagree about the parsed dict shape, so a battery that read
        Terraform through another parser while claiming the 7.3.1 pin would be
        measuring a different program than the one it names.
        """
        import importlib.metadata

        installed = importlib.metadata.version("python-hcl2")
        self.assertEqual(installed, parity.PYTHON_HCL2_PIN)
        declared = (REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8")
        self.assertIn(f'"python-hcl2=={parity.PYTHON_HCL2_PIN}"', declared)
        major = parity.PYTHON_HCL2_PIN.split(".")[0]
        self.assertEqual(major, "7", "the 8.x dict shape is a different contract")


class RuntimeVerifyVerbTests(unittest.TestCase):
    """The battery consumes cli.py's verbs; it never redefines them."""

    def test_every_verb_the_battery_names_exists_in_the_cli(self):
        parser = cli.build_parser()
        for stage, verb in parity.RUNTIME_VERIFY_VERBS.items():
            with self.subTest(stage=stage.value):
                argv = parity.runtime_verify_argv(
                    release=Path("/tmp/release"),
                    task_id="task__x",
                    stage=stage,
                    destination_credential=Path("/tmp/cred.json"),
                    population="counterfactual",
                    destination="databricks",
                )
                self.assertEqual(argv[1], verb)
                args = parser.parse_args(list(argv))
                self.assertEqual(args.task_id, "task__x")
                self.assertEqual(args.population, "counterfactual")
                self.assertEqual(args.destination, "databricks")
                self.assertTrue(args.certification_strict)
                self.assertFalse(args.curator_details)
                self.assertEqual(args.destination_credential, Path("/tmp/cred.json"))

    def test_stage1_carries_the_repetition_argument_and_others_do_not(self):
        stage1 = parity.runtime_verify_argv(
            release="/r",
            task_id="t",
            stage=parity.ReplayStage.STAGE1,
            destination_credential="/c.json",
            expected_repetitions=2,
        )
        self.assertIn("--expected-repetitions", stage1)
        self.assertIn("2", stage1)
        stage2 = parity.runtime_verify_argv(
            release="/r",
            task_id="t",
            stage=parity.ReplayStage.STAGE2,
            destination_credential="/c.json",
        )
        self.assertNotIn("--expected-repetitions", stage2)

    def test_the_credential_enters_as_a_path_and_details_never_do(self):
        argv = parity.runtime_verify_argv(
            release="/r",
            task_id="t",
            stage=parity.ReplayStage.END_TO_END,
            destination_credential="/secrets/scoped_credential.json",
        )
        self.assertNotIn("--curator-details", argv)
        self.assertIn("/secrets/scoped_credential.json", argv)
        with self.assertRaises(parity.ParityBatteryError):
            parity.runtime_verify_argv(
                release="/r",
                task_id="t",
                stage=parity.ReplayStage.END_TO_END,
                destination_credential={"password": "hunter2"},
            )
        with self.assertRaises(parity.ParityBatteryError):
            parity.runtime_verify_argv(
                release="/r",
                task_id="t",
                stage=parity.ReplayStage.END_TO_END,
                destination_credential="/c.json",
                population="not_a_population",
            )

    def test_verify_receipts_become_outcomes_and_exit_two_is_a_fault(self):
        accepted = parity.interpret_verify_payload(
            parity.ReplayStage.STAGE1,
            0,
            json.dumps(
                {"stage": "el", "passed": True, "reward": 1.0, "certification_passed": True}
            ),
        )
        self.assertIs(accepted.accepted, True)
        self.assertEqual(accepted.reward, 1.0)

        rejected = parity.interpret_verify_payload(
            parity.ReplayStage.STAGE2,
            1,
            json.dumps(
                {"stage": "t", "passed": False, "reward": 0.5, "certification_passed": False}
            ),
        )
        self.assertIs(rejected.accepted, False)
        self.assertEqual(rejected.reward, 0.5)

        # Exit 2 is the harness's "could not measure": reward None, never a
        # rejection (C7).
        fault = parity.interpret_verify_payload(parity.ReplayStage.STAGE1, 2, "")
        self.assertIsNone(fault.accepted)
        self.assertIsNone(fault.reward)
        self.assertEqual(fault.fault_code, "runner_exit_2")

        for label, (code, text) in {
            "garbage": (0, "Traceback (most recent call last)"),
            "wrong stage": (0, json.dumps({"stage": "t", "reward": 1.0, "certification_passed": True})),
            "exit disagrees with receipt": (
                1,
                json.dumps({"stage": "el", "reward": 1.0, "certification_passed": True}),
            ),
            "missing certification bit": (0, json.dumps({"stage": "el", "reward": 1.0})),
        }.items():
            with self.subTest(receipt=label):
                outcome = parity.interpret_verify_payload(
                    parity.ReplayStage.STAGE1, code, text
                )
                self.assertIsNone(outcome.accepted)
                self.assertEqual(outcome.fault_code, "unparseable_receipt")

        loose = parity.interpret_verify_payload(
            parity.ReplayStage.STAGE1,
            0,
            json.dumps({"stage": "el", "reward": 1.0}),
            certification_strict=False,
        )
        self.assertIs(loose.accepted, True)


class DifferentialReplayTests(unittest.TestCase):
    """Both runners are injected; nothing here starts a process."""

    def specimen(self, **overrides) -> parity.ReplaySpecimen:
        base = {
            "artifact_id": "P2-dbt-acc-0",
            "arm": "P2",
            "destination": "snowflake",
            "source_pool": "dbt",
            "population": "primary",
            "stage": parity.ReplayStage.END_TO_END,
            "local_accepted": True,
        }
        base.update(overrides)
        return parity.ReplaySpecimen(**base)

    def test_the_injected_destination_runner_produces_the_verdict(self):
        def runner(specimen):
            return parity.ReplayOutcome(
                stage=specimen.stage, accepted=True, reward=1.0, fault_code=""
            )

        rows = parity.differential_replay(
            [self.specimen(artifact_id="b"), self.specimen(artifact_id="a")],
            destination_runner=runner,
        )
        self.assertEqual([row.artifact_id for row in rows], ["a", "b"])
        self.assertTrue(all(row.real_accepted for row in rows))

    def test_a_runner_exception_becomes_reward_none_not_a_rejection(self):
        def crashing(specimen):
            raise OSError("the network is a lie")

        def declared(specimen):
            raise parity.ParityRuntimeFault("runner_timeout")

        crashed = parity.replay_specimen(
            self.specimen(), destination_runner=crashing
        )
        self.assertIsNone(crashed.real_accepted)
        self.assertIsNone(crashed.reward)
        self.assertEqual(crashed.fault_code, "runner_crash")

        timed_out = parity.replay_specimen(
            self.specimen(), destination_runner=declared
        )
        self.assertEqual(timed_out.fault_code, "runner_timeout")
        self.assertIsNone(timed_out.reward)

        # A fault never contributes to pi_c.
        report = parity.parity_rate(
            [crashed]
            + [
                row
                for row in clean_sample()
                if row.artifact_id != crashed.artifact_id
            ]
        )
        self.assertEqual(report.runtime_faults, 1)
        self.assertEqual(report.disagreements, 0)

    def test_terraform_rejection_short_circuits_the_destination(self):
        called: list[str] = []

        def destination(specimen):
            called.append(specimen.artifact_id)
            return parity.ReplayOutcome(
                stage=specimen.stage, accepted=True, reward=1.0, fault_code=""
            )

        def terraform(specimen):
            return json.dumps(terraform_document(errors=1))

        row = parity.replay_specimen(
            self.specimen(terraform_dir=Path("/tmp/elt")),
            destination_runner=destination,
            terraform_runner=terraform,
        )
        self.assertIs(row.real_accepted, False)
        self.assertEqual(row.reward, 0.0)
        self.assertEqual(called, [], "no destination is touched for invalid HCL")
        self.assertTrue(row.disagreement)

    def test_unreadable_terraform_output_is_a_fault_not_a_rejection(self):
        row = parity.replay_specimen(
            self.specimen(terraform_dir=Path("/tmp/elt")),
            destination_runner=lambda specimen: parity.ReplayOutcome(
                stage=specimen.stage, accepted=True, reward=1.0, fault_code=""
            ),
            terraform_runner=lambda specimen: "not json at all",
        )
        self.assertIsNone(row.real_accepted)
        self.assertEqual(row.fault_code, "unparseable_receipt")

    def test_a_runner_answering_for_the_wrong_stage_is_a_fault(self):
        row = parity.replay_specimen(
            self.specimen(stage=parity.ReplayStage.STAGE1),
            destination_runner=lambda specimen: parity.ReplayOutcome(
                stage=parity.ReplayStage.STAGE2,
                accepted=True,
                reward=1.0,
                fault_code="",
            ),
        )
        self.assertIsNone(row.real_accepted)
        self.assertEqual(row.fault_code, "unparseable_receipt")


# ---------------------------------------------------------------------------
# Component 2: proxy-rule mutation
# ---------------------------------------------------------------------------

class MutationBatteryTests(_FixtureMixin, unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.load_fixture()
        cls.cases = parity.default_comparator_cases(
            cls.marts["customer_rollup"],
            cls.gold["customer_rollup"],
            numeric_column="total_spend",
        )

    def test_the_switchable_comparator_is_the_reward_comparator_at_defaults(self):
        """The mutants must differ from THE comparator in exactly one rule.

        That only holds if the all-rules-on form reproduces
        `upstream_eval.compare_mart` byte for byte on every case; otherwise this
        module would be a second reward implementation wearing a disguise.
        """
        for case in self.cases:
            with self.subTest(case=case.case_id):
                self.assertEqual(
                    parity.compare_mart_with_rules(case, parity.REFERENCE_RULES),
                    parity.reference_comparator(case),
                )

    def test_the_switchable_comparator_agrees_over_randomized_shapes(self):
        """The equivalence holds beyond the ten hand-built cases.

        The hand-built cases were chosen to separate rules, so they are exactly
        the shapes most likely to have been fitted to.  This seeded sweep walks
        empty results, ragged rows, extra and missing columns, nulls, numeric
        and textual columns, and undeclared rosters, and demands byte-identical
        verdicts from `compare_mart` and the all-rules-on form on every one.
        """
        import random

        mart = self.marts["customer_rollup"]
        columns = tuple(column.name for column in mart.columns)
        rng = random.Random(7)
        for trial in range(1500):
            gold_rows = [
                {
                    columns[0]: str(index),
                    columns[1]: rng.choice(["1", "2", None, "x"]),
                    columns[2]: rng.choice(["1.0", "2.5", None, "y"]),
                }
                for index in range(rng.randint(0, 4))
            ]
            gold_csv = upstream_eval.rows_to_canonical_csv(gold_rows, columns)
            keys = rng.choice([columns, columns[:2], columns + ("extra",)])
            rows = [
                {name: rng.choice([str(index), "1", None, "a", 2.5]) for name in keys}
                for index in range(rng.randint(0, 4))
            ]
            described = rng.choice([columns, keys, ()])
            case = parity.ComparatorCase(
                f"fuzz-{trial}", mart, gold_csv, tuple(rows), tuple(described)
            )
            if parity.compare_mart_with_rules(
                case, parity.REFERENCE_RULES
            ) != parity.reference_comparator(case):
                self.fail(
                    f"trial {trial} diverges from compare_mart: "
                    f"gold={gold_csv!r} rows={rows!r} described={described!r}"
                )

    def test_the_case_set_kills_every_mutant(self):
        report = parity.run_mutation_battery(self.cases)
        self.assertEqual(report.survived, ())
        self.assertEqual(report.mutation_score, 1.0)
        self.assertEqual(set(report.killed), set(parity.COMPARATOR_MUTANTS))
        # Each mutant is killed by the case built to separate exactly it.
        self.assertEqual(
            report.killing_case,
            {
                "no_column_roster_rule": "undeclared_extra_column",
                "no_missing_column_rule": "missing_gold_column",
                "no_null_asymmetry_rule": "null_for_value",
                "no_ragged_row_rule": "ragged_row",
                "no_row_count_rule": "dropped_row",
                "no_total_order_sort_rule": "reordered_rows",
                "wide_numeric_tolerance": "drift_beyond_tolerance",
            },
        )

    def test_a_weak_case_set_lets_mutants_survive(self):
        """The battery's power comes from the cases, and it says so.

        Running only the identity case cannot separate anything, and the report
        must show that rather than printing a clean score.
        """
        identity = [case for case in self.cases if case.case_id == "identity"]
        report = parity.run_mutation_battery(identity)
        self.assertEqual(report.killed, ())
        self.assertEqual(set(report.survived), set(parity.COMPARATOR_MUTANTS))
        self.assertEqual(report.mutation_score, 0.0)

    def test_default_cases_refuse_an_unusable_gold_mart(self):
        with self.assertRaises(parity.ParityBatteryError):
            parity.default_comparator_cases(
                self.marts["customer_rollup"],
                self.gold["customer_rollup"],
                numeric_column="customer_id",  # a key column
            )
        with self.assertRaises(parity.ParityBatteryError):
            parity.default_comparator_cases(
                self.marts["customer_rollup"],
                "customer_id,completed_orders,total_spend\n1,1,1.0\n",
                numeric_column="total_spend",
            )


class AcceptanceLaneTests(_FixtureMixin, unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.load_fixture()
        cls.cases = parity.default_comparator_cases(
            cls.marts["customer_rollup"],
            cls.gold["customer_rollup"],
            numeric_column="total_spend",
        )
        cls.findings = (
            parity.MetamorphicFinding(case_id="tlp_spend", relation="tlp", holds=True),
            parity.MetamorphicFinding(case_id="norec_orders", relation="norec", holds=True),
        )

    def lane(self, **overrides):
        kwargs = {
            "comparator_cases": self.cases,
            "metamorphic_findings": self.findings,
            "verdicts": clean_sample(),
        }
        kwargs.update(overrides)
        return parity.run_acceptance_lane(**kwargs)

    def test_the_clean_lane_passes(self):
        result = self.lane()
        self.assertTrue(result.passed, result.reasons)
        self.assertEqual(result.reasons, ())
        self.assertEqual(result.mutation.survived, ())
        self.assertTrue(result.parity.adopted)

    def test_emulator_mutation_fails_the_acceptance_lane(self):
        """Mutate the local comparator and the lane must refuse the arm.

        This is the component that makes the rest of the battery mean something.
        A weakened proxy comparator accepts artifacts the real stack would
        reject, so it inflates agreement exactly where parity is supposed to be
        measured.  Two independent guards fire for every mutant: the lane's
        comparator no longer agrees with THE reward comparator, and the mutation
        battery cannot kill the mutation the lane is itself running -- a
        comparator can never separate itself from a copy of itself.
        """
        clean = self.lane()
        self.assertTrue(clean.passed)

        for name in sorted(parity.COMPARATOR_MUTANTS):
            with self.subTest(mutant=name):
                mutated = parity.mutant_comparator(name)
                result = self.lane(comparator=mutated)
                self.assertFalse(
                    result.passed,
                    f"the acceptance lane accepted the {name} mutation",
                )
                self.assertIn(f"mutant_survived:{name}", result.reasons)
                self.assertIn(name, result.mutation.survived)
                self.assertTrue(
                    any(
                        reason.startswith(
                            "comparator_disagrees_with_reward_comparator:"
                        )
                        for reason in result.reasons
                    ),
                    result.reasons,
                )
                # The mutation is a comparator defect, not a sampling defect:
                # the parity arithmetic itself is untouched.
                self.assertEqual(result.parity.disagreements, 0)
                self.assertEqual(result.parity.sizing_shortfalls, ())

    def test_a_metamorphic_violation_fails_the_lane(self):
        result = self.lane(
            metamorphic_findings=(
                parity.MetamorphicFinding(
                    case_id="tlp_spend",
                    relation="tlp",
                    holds=False,
                    code="tlp_partition_disagrees",
                ),
            )
        )
        self.assertFalse(result.passed)
        self.assertIn("metamorphic_violation:tlp_spend", result.reasons)

    def test_an_unmeasured_metamorphic_probe_is_not_a_violation(self):
        result = self.lane(
            metamorphic_findings=(
                parity.MetamorphicFinding(
                    case_id="tlp_spend",
                    relation="tlp",
                    holds=None,
                    code="query_failed",
                ),
            )
        )
        self.assertFalse(result.passed)
        self.assertIn("metamorphic_unmeasured:tlp_spend", result.reasons)
        self.assertNotIn("metamorphic_violation:tlp_spend", result.reasons)

    def test_a_real_disagreement_below_the_threshold_fails_the_lane(self):
        rows = list(clean_sample())
        for index in range(4):
            target = rows[index]
            rows[index] = verdict(
                target.artifact_id,
                pool=target.source_pool,
                local=True,
                real=False,
            )
        result = self.lane(verdicts=rows)
        self.assertFalse(result.passed)
        self.assertIn("agreement_lower_bound_below_threshold", result.reasons)
        self.assertEqual(len(result.parity.quarantine_artifact_ids), 4)

    def test_an_empty_lane_refuses_rather_than_passing_vacuously(self):
        result = parity.run_acceptance_lane()
        self.assertFalse(result.passed)
        self.assertEqual(
            result.reasons,
            ("no_comparator_cases", "no_metamorphic_findings", "no_verdicts"),
        )
        # Each component missing is its own reason: a lane that ran three of the
        # four must not read as a pass.
        self.assertFalse(self.lane(metamorphic_findings=()).passed)
        self.assertIn(
            "no_metamorphic_findings", self.lane(metamorphic_findings=()).reasons
        )


# ---------------------------------------------------------------------------
# Component 3: metamorphic TLP and NoREC over the DuckDB comparator
# ---------------------------------------------------------------------------

class _BrokenPartitionConnection:
    """A connection that drops TLP's `IS NULL` arm before executing.

    The classic partition bug: two-valued thinking in a three-valued logic.
    Rows whose predicate evaluates to NULL fall out of the rewrite entirely, so
    the oracle must catch it.
    """

    def __init__(self, inner) -> None:
        self._inner = inner

    def execute(self, sql: str):
        marker = " UNION ALL SELECT"
        if sql.count(marker) == 2:
            sql = sql[: sql.rindex(marker)]
        return self._inner.execute(sql)


class _FailingConnection:
    def execute(self, sql: str):
        raise duckdb.Error("the probe could not run")


class MetamorphicFixtureTests(_FixtureMixin, unittest.TestCase):
    """TLP and NoREC on the COMMITTED five-backend benchmark fixture."""

    WIDE_COLUMNS = (
        "event_id, big_count, label, occurred_at, tz_stamp, metric_value, big_note"
    )

    @classmethod
    def setUpClass(cls) -> None:
        cls.load_fixture()

    def setUp(self) -> None:
        self.connection = self.open_fixture_duckdb()
        self.addCleanup(self.connection.close)

    def tlp_cases(self) -> tuple[parity.MetamorphicCase, ...]:
        rollup = self.marts["customer_rollup"]
        wide = self.marts["event_wide"]
        return (
            parity.MetamorphicCase(
                "tlp_rollup_total_spend",
                "tlp",
                rollup,
                "customer_id, completed_orders, total_spend",
                "customer_rollup",
                "total_spend > 50",
            ),
            parity.MetamorphicCase(
                "tlp_rollup_completed_orders",
                "tlp",
                rollup,
                "customer_id, completed_orders, total_spend",
                "customer_rollup",
                "completed_orders = 1",
            ),
            # metric_value is NULL for the events the LEFT JOIN does not match,
            # so this predicate is NULL for real rows: the `IS NULL` arm is
            # load-bearing here, not decorative.
            parity.MetamorphicCase(
                "tlp_wide_metric_value",
                "tlp",
                wide,
                self.WIDE_COLUMNS,
                "event_wide",
                "metric_value > 1",
            ),
            parity.MetamorphicCase(
                "tlp_wide_label",
                "tlp",
                wide,
                self.WIDE_COLUMNS,
                "event_wide",
                "label = 'alpha'",
            ),
        )

    def norec_cases(self) -> tuple[parity.MetamorphicCase, ...]:
        rollup = self.marts["customer_rollup"]
        wide = self.marts["event_wide"]
        return (
            parity.MetamorphicCase(
                "norec_rollup_spend",
                "norec",
                rollup,
                "customer_id, completed_orders, total_spend",
                "customer_rollup",
                "total_spend > 50",
            ),
            parity.MetamorphicCase(
                "norec_wide_metric_present",
                "norec",
                wide,
                "event_id",
                "event_wide",
                "metric_value IS NOT NULL",
            ),
            parity.MetamorphicCase(
                "norec_wide_metric_value",
                "norec",
                wide,
                "event_id",
                "event_wide",
                "metric_value > 1",
            ),
        )

    def test_the_fixture_has_null_valued_predicates_to_partition_on(self):
        """Without a three-valued predicate the TLP oracle proves nothing."""
        rows = self.connection.execute(
            "SELECT COUNT(*) FROM event_wide WHERE (metric_value > 1) IS NULL"
        ).fetchall()
        self.assertGreater(int(rows[0][0]), 0)

    def test_tlp_partitions_reproduce_the_relation_on_the_fixture(self):
        findings = parity.run_metamorphic_checks(self.connection, self.tlp_cases())
        self.assertEqual(len(findings), 4)
        for finding in findings:
            with self.subTest(case=finding.case_id):
                self.assertIs(finding.holds, True, finding.code)
                self.assertEqual(finding.code, "")
                self.assertEqual(finding.relation, "tlp")

    def test_norec_rewrites_agree_on_the_fixture(self):
        findings = parity.run_metamorphic_checks(self.connection, self.norec_cases())
        self.assertEqual(len(findings), 3)
        for finding in findings:
            with self.subTest(case=finding.case_id):
                self.assertIs(finding.holds, True, finding.code)
                self.assertEqual(finding.relation, "norec")

    def test_a_two_valued_tlp_rewrite_is_caught(self):
        """Dropping the `IS NULL` arm must be a violation, not a pass."""
        broken = _BrokenPartitionConnection(self.connection)
        findings = parity.run_metamorphic_checks(broken, self.tlp_cases())
        by_case = {finding.case_id: finding for finding in findings}
        self.assertIs(by_case["tlp_wide_metric_value"].holds, False)
        self.assertEqual(
            by_case["tlp_wide_metric_value"].code, "tlp_partition_disagrees"
        )
        # A predicate that is never NULL is unaffected: the oracle bites where
        # three-valued logic actually applies, not everywhere.
        self.assertIs(by_case["tlp_rollup_completed_orders"].holds, True)

    def test_a_failing_probe_is_unmeasured_rather_than_violated(self):
        findings = parity.run_metamorphic_checks(
            _FailingConnection(), self.tlp_cases() + self.norec_cases()
        )
        for finding in findings:
            with self.subTest(case=finding.case_id):
                self.assertIsNone(finding.holds)
                self.assertEqual(finding.code, "query_failed")

    def test_the_metamorphic_check_runs_through_the_reward_comparator(self):
        """TLP is judged by `compare_mart`, not by a private row equality."""
        seen: list[str] = []

        def judge(case: parity.ComparatorCase) -> bool:
            seen.append(case.case_id)
            return parity.reference_comparator(case)

        parity.run_metamorphic_checks(
            self.connection, self.tlp_cases()[:1], comparator=judge
        )
        self.assertEqual(seen, ["tlp_rollup_total_spend"])

    def test_the_rewrites_are_the_documented_sql_shapes(self):
        partition = parity.tlp_partition_sql("a", "t", "p")
        self.assertEqual(
            partition,
            "SELECT a FROM t WHERE (p) UNION ALL SELECT a FROM t WHERE NOT (p) "
            "UNION ALL SELECT a FROM t WHERE (p) IS NULL",
        )
        self.assertIn("IS TRUE", parity.norec_unoptimized_sql("t", "p"))
        self.assertIn("COUNT(*)", parity.norec_optimized_sql("a", "t", "p"))
        with self.assertRaises(parity.ParityBatteryError):
            parity.MetamorphicCase(
                "x", "fuzz", self.marts["customer_rollup"], "a", "t", "p"
            )

    def test_the_committed_reference_mart_still_matches_its_frozen_gold(self):
        """A fixture whose reference no longer reproduces gold proves nothing."""
        cursor = self.connection.execute("SELECT * FROM customer_rollup")
        columns = tuple(item[0] for item in cursor.description)
        rows = [dict(zip(columns, values)) for values in cursor.fetchall()]
        self.assertTrue(
            upstream_eval.compare_mart(
                self.gold["customer_rollup"],
                rows,
                self.marts["customer_rollup"],
                actual_columns=columns,
            )
        )


# ---------------------------------------------------------------------------
# tools/parity_sample.py
# ---------------------------------------------------------------------------

class ParitySampleToolTests(unittest.TestCase):
    """The tool is a JSON-in/JSON-out step; it opens no credential and no socket."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.tool = _load_tool()

    def run_tool(self, argv: list[str]) -> tuple[int, str]:
        """Run one step with its console output captured, not printed."""
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(
            io.StringIO()
        ):
            code = self.tool.main(argv)
        return code, stdout.getvalue()

    def candidates(self) -> list[dict]:
        rows: list[dict] = []
        for pool in SAMPLE_POOLS:
            for index in range(10):
                rows.append(
                    {
                        "artifact_id": f"P2-{pool}-acc-{index}",
                        "arm": "P2",
                        "destination": "snowflake",
                        "source_pool": pool,
                        "population": "primary",
                        "stage": "end_to_end",
                        "local_accepted": True,
                    }
                )
            for index in range(5):
                rows.append(
                    {
                        "artifact_id": f"P2-{pool}-rej-{index}",
                        "arm": "P2",
                        "destination": "snowflake",
                        "source_pool": pool,
                        "population": "primary",
                        "stage": "end_to_end",
                        "local_accepted": False,
                    }
                )
        return rows

    def test_the_sample_reaches_the_table_9_floors_and_is_deterministic(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            candidates = root / "candidates.json"
            candidates.write_text(json.dumps(self.candidates()), encoding="utf-8")
            first = root / "sample1.json"
            second = root / "sample2.json"
            for out in (first, second):
                code, _ = self.run_tool(
                    [
                        "sample",
                        "--candidates",
                        str(candidates),
                        "--out",
                        str(out),
                        "--require-floors",
                    ]
                )
                self.assertEqual(code, 0)
            self.assertEqual(
                first.read_text(encoding="utf-8"), second.read_text(encoding="utf-8")
            )
            drawn = json.loads(first.read_text(encoding="utf-8"))
        accepted = [row for row in drawn if row["local_accepted"]]
        rejected = [row for row in drawn if not row["local_accepted"]]
        self.assertGreaterEqual(len(accepted), parity.MIN_LOCALLY_ACCEPTED_PER_ARM)
        self.assertGreaterEqual(len(rejected), parity.MIN_LOCALLY_REJECTED_PER_ARM)
        for pool in SAMPLE_POOLS:
            self.assertGreaterEqual(
                sum(1 for row in drawn if row["source_pool"] == pool),
                parity.MIN_ARTIFACTS_PER_SOURCE_POOL,
            )

    def test_a_thin_candidate_pool_is_refused_under_require_floors(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            candidates = root / "candidates.json"
            candidates.write_text(
                json.dumps(self.candidates()[:6]), encoding="utf-8"
            )
            code, _ = self.run_tool(
                [
                    "sample",
                    "--candidates",
                    str(candidates),
                    "--out",
                    str(root / "sample.json"),
                    "--require-floors",
                ]
            )
        self.assertEqual(code, 2)

    def test_report_publishes_one_file_per_arm(self):
        rows = [row.model_dump(mode="json") for row in clean_sample("P2")]
        rows += [row.model_dump(mode="json") for row in clean_sample("P3")]
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            verdicts = root / "verdicts.json"
            verdicts.write_text(json.dumps(rows), encoding="utf-8")
            code, printed = self.run_tool(
                [
                    "report",
                    "--verdicts",
                    str(verdicts),
                    "--reports-dir",
                    str(root / "reports"),
                ]
            )
            self.assertEqual(code, 0)
            self.assertIn("named population set", printed)
            names = sorted(path.name for path in (root / "reports").iterdir())
        self.assertEqual(names, ["parity_P2.json", "parity_P3.json"])

    def test_report_names_the_attestation_the_owner_supplies(self):
        """The owner's real-destination run has an attestation; the tool takes it.

        `--attestation` is how the sealed record reaches the published report,
        and an unreadable or unsealed one REFUSES (exit 2) rather than
        publishing an unattributed claim.  The file is a public, secret-free
        document by construction, so reading it breaks no rule that reading a
        credential would.
        """
        record = attestation_fixture.attestation(tier="A")
        rows = [row.model_dump(mode="json") for row in clean_sample("P2")]
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            verdicts = root / "verdicts.json"
            verdicts.write_text(json.dumps(rows), encoding="utf-8")
            sealed = root / "sandbox_attestation.json"
            sealed.write_text(
                json.dumps(record.model_dump(mode="json")), encoding="utf-8"
            )
            code, _ = self.run_tool(
                [
                    "report",
                    "--verdicts",
                    str(verdicts),
                    "--reports-dir",
                    str(root / "reports"),
                    "--attestation",
                    str(sealed),
                ]
            )
            self.assertEqual(code, 0)
            published = json.loads(
                (root / "reports" / "parity_P2.json").read_text(encoding="utf-8")
            )
            self.assertEqual(
                published["sandbox_attestation_digest"], record.attestation_digest
            )
            parity.verify_parity_report(
                parity.ParityReport.model_validate(published)
            )

            # An edited record is refused; nothing is published for it.
            broken = root / "broken.json"
            payload = record.model_dump(mode="json")
            payload["tier"] = "B"
            broken.write_text(json.dumps(payload), encoding="utf-8")
            code, _ = self.run_tool(
                [
                    "report",
                    "--verdicts",
                    str(verdicts),
                    "--reports-dir",
                    str(root / "refused"),
                    "--attestation",
                    str(broken),
                ]
            )
            self.assertEqual(code, 2)
            self.assertFalse((root / "refused").exists())

            # A missing file is a refusal, not a silently unattested report.
            code, _ = self.run_tool(
                [
                    "report",
                    "--verdicts",
                    str(verdicts),
                    "--attestation",
                    str(root / "absent.json"),
                ]
            )
            self.assertEqual(code, 2)

    def test_argv_emits_the_verify_command_lines_without_touching_a_credential(self):
        specimens = [
            {
                "artifact_id": "P2-dbt-acc-0",
                "arm": "P2",
                "destination": "snowflake",
                "source_pool": "dbt",
                "population": "primary",
                "stage": "stage1",
                "local_accepted": True,
            }
        ]
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            sample = root / "sample.json"
            sample.write_text(json.dumps(specimens), encoding="utf-8")
            missing_credential = root / "never-created.json"
            code, printed = self.run_tool(
                [
                    "argv",
                    "--sample",
                    str(sample),
                    "--release",
                    str(root / "release"),
                    "--task-id",
                    "task__x",
                    "--destination-credential",
                    str(missing_credential),
                ]
            )
            self.assertEqual(code, 0)
            # The credential is a path in argv and was never opened.
            self.assertFalse(missing_credential.exists())
        line = json.loads(printed.strip())
        self.assertEqual(line["argv"][:2], ["runtime", "verify-stage1"])
        self.assertIn(str(missing_credential), line["argv"])

    def test_a_malformed_manifest_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "candidates.json"
            path.write_text(json.dumps({"not": "a list"}), encoding="utf-8")
            self.assertEqual(
                self.run_tool(["sample", "--candidates", str(path)])[0], 2
            )
            path.write_text(
                json.dumps([{"artifact_id": "a"}]), encoding="utf-8"
            )
            self.assertEqual(
                self.run_tool(["sample", "--candidates", str(path)])[0], 2
            )
            # A misspelled source pool would otherwise become its own stratum
            # and silently satisfy nothing.
            typo = dict(self.candidates()[0], source_pool="dbtt")
            path.write_text(json.dumps([typo]), encoding="utf-8")
            self.assertEqual(
                self.run_tool(["sample", "--candidates", str(path)])[0], 2
            )


class RepairPassParityTests(unittest.TestCase):
    """The Phase 4/5 repair pass: findings p5-4 and p5-5."""

    def test_sizing_floor_is_measured_after_runtime_faults(self) -> None:
        """finding p5-4: the evidence base is what the REAL arm measured.

        `stratification_shortfalls` counts `local_accepted` — the DRAWN sample,
        which is what the sampler must check before anything is replayed — and
        never reads `measured`, while the denominator is built from `judged`
        only. Runtime faults therefore left the denominator without ever
        registering as a shortfall, and an arm with 12 real measurements
        against a Table 9 floor of 20 reported `shortfalls: ()` and
        `adopted: True`.
        """
        rows = list(clean_sample("B1"))
        # Fault out enough of the accepted rows to drop under the floor.
        faulted = 0
        rebuilt: list[parity.ArtifactVerdict] = []
        for row in rows:
            if row.local_accepted and faulted < 8:
                faulted += 1
                rebuilt.append(
                    verdict(
                        row.artifact_id,
                        arm=row.arm,
                        pool=row.source_pool,
                        local=True,
                        real=None,
                        fault_code="connection_error",
                    )
                )
            else:
                rebuilt.append(row)
        report = parity.parity_rate(rebuilt)
        self.assertEqual(report.locally_accepted, 20)
        self.assertEqual(report.runtime_faults, 8)
        self.assertEqual(report.denominator, 12)
        # The DRAWN sample still meets the accepted floor: the shortfall the
        # sampler checks is silent, exactly as before.
        self.assertNotIn(
            f"accepted_below_minimum:B1:20/{parity.MIN_LOCALLY_ACCEPTED_PER_ARM}",
            report.sizing_shortfalls,
        )
        # ...and the agreement bound over the 12 that were measured is high.
        self.assertGreaterEqual(report.agreement_lower, 0.9)
        # THE FIX: the post-fault denominator is what powers the claim.
        self.assertIn(
            f"denominator_below_minimum:B1:12/{parity.MIN_LOCALLY_ACCEPTED_PER_ARM}",
            report.sizing_shortfalls,
        )
        self.assertFalse(report.adopted)
        # ...and the acceptance lane reports it as a parity reason.
        lane = parity.run_acceptance_lane(verdicts=rebuilt)
        self.assertIn(
            "sample_shortfall:denominator_below_minimum:B1:12/"
            f"{parity.MIN_LOCALLY_ACCEPTED_PER_ARM}",
            lane.reasons,
        )
        # A fully measured sample of the same shape is adopted, so the new
        # code is the FAULTS talking and not a blanket refusal.
        whole = parity.parity_rate(list(clean_sample("B1")))
        self.assertEqual(whole.sizing_shortfalls, ())
        self.assertTrue(whole.adopted)

    def test_population_set_names_the_measured_frame_only(self) -> None:
        """finding p5-5: the named set and the bound beside it must range over
        the same artifacts.

        `PopulationSet.artifact_ids` enumerated every LOCALLY ACCEPTED
        artifact, faults included, while `agreement_lower` was computed over
        the measured subset — so a reader quoting `report.population_set`
        beside `report.agreement_lower`, both first-class sealed fields,
        attributed a 12-artifact bound to a 20-artifact frame.
        """
        rows = [verdict(f"m{i}", arm="B1", local=True, real=True) for i in range(4)]
        rows += [
            verdict(
                f"f{i}",
                arm="B1",
                local=True,
                real=None,
                fault_code="connection_error",
            )
            for i in range(6)
        ]
        report = parity.parity_rate(rows)
        self.assertEqual(report.denominator, 4)
        self.assertEqual(report.population_set.size, 4)
        self.assertEqual(
            report.population_set.artifact_ids, tuple(f"m{i}" for i in range(4))
        )
        self.assertEqual(
            report.population_set.unmeasured_artifact_ids,
            tuple(sorted(f"f{i}" for i in range(6))),
        )
        # The model itself refuses a report whose named set outruns its bound.
        payload = report.model_dump(mode="json")
        payload["population_set"]["artifact_ids"] = sorted(
            [*report.population_set.artifact_ids, "f0"]
        )
        payload["population_set"]["size"] = 5
        payload["population_set"]["unmeasured_artifact_ids"] = sorted(
            set(report.population_set.unmeasured_artifact_ids) - {"f0"}
        )
        with self.assertRaises(Exception):
            parity.ParityReport.model_validate(payload)

    def test_population_set_name_is_an_annotation_not_a_replacement(self) -> None:
        """finding p5-5, second half: free text may ANNOTATE the derived frame
        description, never replace it — the only consistency check is that the
        claim names the set, and the claim is built to satisfy it."""
        rows = [verdict(f"m{i}", arm="B1", local=True, real=True) for i in range(3)]
        report = parity.parity_rate(rows, population_set_name="every P2 artifact")
        self.assertTrue(report.population_set.name.startswith("arm=B1;"))
        self.assertIn("n=3", report.population_set.name)
        self.assertIn("(every P2 artifact)", report.population_set.name)
        self.assertIn(report.population_set.name, report.claim)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
