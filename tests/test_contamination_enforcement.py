"""The contamination layer's enforcement switch (ELT_TASKGEN_CONTAMINATION).

ENFORCE refuses and queues; OBSERVE detects and records but never blocks; OFF
skips detection. Default is ENFORCE, and an unrecognised value falls back to
ENFORCE — a typo must never silently disarm the layer.
"""

from __future__ import annotations

import unittest
from pathlib import Path

from elt_taskgen.demo_fixture import demo_task
from elt_taskgen.verification import contamination as cont
from elt_taskgen.verification import gates


class ModeResolution(unittest.TestCase):
    def test_default_is_enforce(self) -> None:
        self.assertIs(cont.enforcement({}), cont.Enforcement.ENFORCE)
        self.assertTrue(cont.enforcing({}))

    def test_observe_and_off_do_not_enforce(self) -> None:
        for raw in ("observe", "record", "warn", "audit"):
            self.assertIs(
                cont.enforcement({cont.ENFORCEMENT_ENV: raw}),
                cont.Enforcement.OBSERVE,
            )
        for raw in ("off", "0", "false", "no", "disabled", "none"):
            self.assertIs(
                cont.enforcement({cont.ENFORCEMENT_ENV: raw}), cont.Enforcement.OFF
            )
        for raw in ("observe", "off"):
            self.assertFalse(cont.enforcing({cont.ENFORCEMENT_ENV: raw}))

    def test_an_unrecognised_value_fails_closed(self) -> None:
        for raw in ("obserev", "yes", "1", "true", "enforced", "  "):
            self.assertIs(
                cont.enforcement({cont.ENFORCEMENT_ENV: raw}),
                cont.Enforcement.ENFORCE,
                f"{raw!r} must fall back to ENFORCE",
            )

    def test_case_and_whitespace_insensitive(self) -> None:
        self.assertIs(
            cont.enforcement({cont.ENFORCEMENT_ENV: "  OBSERVE "}),
            cont.Enforcement.OBSERVE,
        )


class CoverageRefusalHonoursTheMode(unittest.TestCase):
    """`coverage_failure` is the shared refusal path for an under-covered index."""

    def _unarmed(self) -> cont.IndexCoverage:
        return cont.IndexCoverage(level=cont.CoverageLevel.UNARMED, index_dir="x")

    def test_enforcing_refuses(self) -> None:
        import os

        prior = os.environ.pop(cont.ENFORCEMENT_ENV, None)
        try:
            self.assertIsNotNone(cont.coverage_failure(self._unarmed()))
        finally:
            if prior is not None:
                os.environ[cont.ENFORCEMENT_ENV] = prior

    def test_observing_does_not_refuse(self) -> None:
        import os

        prior = os.environ.get(cont.ENFORCEMENT_ENV)
        os.environ[cont.ENFORCEMENT_ENV] = "observe"
        try:
            self.assertIsNone(cont.coverage_failure(self._unarmed()))
        finally:
            if prior is None:
                os.environ.pop(cont.ENFORCEMENT_ENV, None)
            else:
                os.environ[cont.ENFORCEMENT_ENV] = prior


class GateHonoursTheMode(unittest.TestCase):
    def test_gate_passes_without_evidence_when_not_enforcing(self) -> None:
        """The strongest form: no evidence file at all still passes, because
        the gate is no longer an admission barrier."""
        import os

        prior = os.environ.get(cont.ENFORCEMENT_ENV)
        os.environ[cont.ENFORCEMENT_ENV] = "observe"
        try:
            r = gates._gate_contamination_clean(demo_task(), Path("/nonexistent"))
            self.assertTrue(r.passed)
            self.assertEqual(r.gate, "contamination-clean")
            self.assertEqual(r.evidence.get("enforcement"), "observe")
        finally:
            if prior is None:
                os.environ.pop(cont.ENFORCEMENT_ENV, None)
            else:
                os.environ[cont.ENFORCEMENT_ENV] = prior

    def test_gate_still_refuses_missing_evidence_when_enforcing(self) -> None:
        import os

        prior = os.environ.pop(cont.ENFORCEMENT_ENV, None)
        try:
            r = gates._gate_contamination_clean(demo_task(), Path("/nonexistent"))
            self.assertFalse(r.passed)
        finally:
            if prior is not None:
                os.environ[cont.ENFORCEMENT_ENV] = prior


if __name__ == "__main__":
    unittest.main()
