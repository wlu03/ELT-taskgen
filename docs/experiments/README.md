# Pilot pre-registration files

This directory contains one `PILOT-<name>.md` file for each bounded-agent
pilot: P1 author, P2 EL loader, P3 T implementer, P4 population adversary, P5
critics, and P6 repair proposer. Each file must be committed before the first
paid call. Phase 1 requires P1 and P6, Phase 2 requires P2 and P3, Phase 3
requires P4, and Phase 4 requires P5. P2 through P5 are registered but have not
run. P1 and P6 are not registered. This file defines the required format.

## What a `PILOT-<name>.md` states

Each file contains these sections in order so results can be checked against
the pre-registered design:

1. **Hypothesis** — the primary contrast (which arm over which) and the
   direction of the effect.
2. **Arms** — each arm's role wiring and caps (`max_model_calls`,
   `max_tool_calls`, `max_wall_s`, `max_usd`, `max_oracle_bits`, and for
   harness-validated seats `max_compile_corrections`), and any pilot-only
   override that exceeds a production hard cap. Record an override only here
   and in that arm's routing fingerprint. It does not change production limits.
3. **Cohort digest** — the sha256 of `task_links.json` plus each task's
   `content_hash`, recomputed on the cohort before any paid call, and whether
   the cohort is disjoint from the five curation drives.
4. **Replication** — `K` samples per unit (or `R` replicates), the seed, and
   the seeded permutation that interleaves arms.
5. **Primary metric and test** — the per-trial score, the paired test, the
   multiple-comparison correction, and the pre-registered interim looks, if
   any.
6. **Adoption thresholds, vetoes, and fallback** — the required effect size
   and p-value, unconditional rejection conditions, and the default action if
   neither arm qualifies.
7. **Exclusion rules** — which trials are dropped before analysis and why,
   fixed before the data exist.
8. **Budget cap** — the total USD limit and per-trial `--budget-per-task`.
   Exceeding either limit stops the pilot as an infrastructure failure with
   exit code 2.
9. **Routing fingerprint per arm** — provider, model, `max_tokens`, effort,
   the system prompt sha256, the tool-manifest sha256 and the `policy_sha256`
   of every arm, computed from the code that will run. Prompt or tool-schema
   changes must change the fingerprint.

## Rules every pilot runs under

* Pilot workspaces must not be under `runs/` or a release directory. The model
  can access only placeholder credentials in its attempt copy
  (`tests/test_credential_sweep.py`).
* Pilot adoption does not admit a critic seat. Admission requires a new live
  `elt-taskgen metrology` run whose fingerprint includes the changed behavior.
* The analysis code (a stdlib-only, deterministic `pilot_stats` module under
  `tools/`) and its tests must be committed with the first pilot file. A free
  offline run must recover a planted effect before any paid call. Neither
  requirement has been completed.
* The files here are current documentation: `tests/test_docs_consistency.py`
  checks their CLI terminology and execution-status label. The implementation
  is a DuckDB semantic proxy with real-runtime adapters, not a vendor emulator.
