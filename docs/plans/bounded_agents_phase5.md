# Phase 5 of the bounded-agents roadmap: sparse cloud certification

Status: shipped offline on 2026-09-04. Phase 5 measures whether artifacts
accepted by the local certifier also pass the real stack, records the isolation
used by a label-bearing run, and prevents live credentials from entering the
model-facing copy. Training has no added cloud dependency: an ordinary RLVR
rollout still performs zero warehouse queries (C2, C8), and this phase makes no
model call. The tests do not start a container, run `runc` or `runsc`, open a
socket, or read a credential file; all host and destination observations are
injected. A 2026-09-04 adversarial-review pass changed §3b and moved
`SANDBOX_ATTESTATION_SCHEMA_VERSION` from `"1.0"` to `"1.1"`. The §4 hand-off
list was otherwise unchanged.

Related phase records: `bounded_agents_phase0.md` (projection layer, fault
taxonomy, metering, fingerprint v5), `bounded_agents_phase1.md` (the bounded
runner, the author session, the F1 fix, the agentic proposer),
`bounded_agents_phase2.md` (the witness sessions under OQ-23 option C, the
training signal and the declarative environment), `bounded_agents_phase3.md`
(the POPULATION route, the default-off critic validators, the correction
channel) and `bounded_agents_phase4.md` (the harness-6 metrology protocol).

Authority: Output 12 §8 (Table 9, its interface block, test list, and definition
of done), threat row A22 controls 2 and 5, and reconciliation R-F. The
fingerprint hashes the pinned sandbox declaration; the observed attestation is
stored and is not hashed.

## 0. The fence, and why this phase is mostly new files

During Phase 5, a concurrent runtime/export effort owned `runtime_matrix.py`,
`runtime/execution.py`, `runtime/install.py`, `export/**`, `runtime-images/**`,
`docs/EXECUTION_MODEL.md`, `docs/WAREHOUSE_CONNECTORS.md`, the `Makefile`,
`pyproject.toml`, `setup.py`, and `build_backend.py`. That effort had rewritten
several of them (certification matrix v11, runner-images manifest v2,
certification attestation schema 1.3, release schema 3.4). Phase 5 therefore used
new modules and tests that consume those files through public interfaces. §4
records each requested change to an owned file, including the file, function,
exact change, and pinning test. Those changes were not made, and §1 through §3
do not modify an owned file.

## 1. What shipped, per item

### Item 1 — `runtime/attestation.py` (new): the sandbox attestation and the fail-closed preflight

`SandboxAttestation` uses the exact Table 9 field list. It is flat, frozen, and
`extra="forbid"`. As specified by `export/certification.py`, its seal is the
SHA-256 digest of the canonical record with the digest field blank. This makes
later edits detectable. Attestations must not contain secrets. Every field is a
version string, digest, bounded label, boolean, or count.
`assert_attestation_is_secret_free` checks values and nested keys for
password, secret, token, or private-key material. Every string field is bounded,
stripped, and free of control characters.

`PreflightResult` is sealed the same way, and its digest is the attestation's
`preflight_sha256`, so the attestation names the observation it was built from.

`isolation_preflight()` fails closed. A required observation that cannot be made
is a failure. `attest_sandbox` does not create a record when the host preflight
fails, so an unsafe host produces no record for the tier gate. The floors are:

| Floor | Value | Advisory |
|---|---|---|
| `runc` | at least `1.4.3`, or the `1.3.x` backport `1.3.6` | GHSA-xjvp-4fhw-gc47 |
| `runsc` | at least `release-20240325.0`; `release-20260817.0` pinned as the recommended tag (advisory) | GHSA-4fj4-9m67-3mj3 |
| kernel | at least `5.6` | — |
| cgroup | v2 with the `systemd` driver | — |
| user namespaces | `kernel.unprivileged_userns_clone=1` OR `user.max_user_namespaces>=1` | either satisfies it; neither readable is a failure |
| Docker runtimes | include `runsc` — the TIER determinant, not a gate | — |
| containerd | recorded, never a sufficient pin | Table 9 says so explicitly |

Tiers: `A` is gVisor on a Linux host meeting every floor; `B` is a hosted
microVM/gVisor sandbox declared by the operator
(`ELT_TASKGEN_SANDBOX_HOST_CLASS`); `C` plain `runc`; `D` macOS, Docker Desktop
or the `dev-only` laptop lane; `0` unclassified, the fail-closed value.
`ELT_TASKGEN_ISOLATION=dev-only` marks a laptop run as non-attesting. It still
creates a tier D record, which the release gate refuses for RLVR labels.

R-F is enforced by `attest_sandbox(*, pinned, preflight)`. It compares the
observed runtime, image digest, and workspace-template digest to the pinned
`metrology.sandbox` declaration read through `providers.sandbox_pin`, and raises
`SandboxPinMismatch` on any disagreement (CLI convention
`ERROR [sandbox_pin_mismatch]`, exit 2, nothing measured). The fingerprint keeps
hashing the pin, so PROOF 3's cross-process determinism still holds on a host
that has never run a container.

Every host fact is supplied through the repository's existing
`runtime.process.Runner` protocol (default: the bounded `SubprocessRunner`), the
process environment, or the platform-name parameter. Only a closed key set is
read from `docker info`; proxy and registry fields, which can contain
credentials, are not read.

`credential_files_in_mount` is counted by name with the shared vocabulary
(`review/tools/credential_sweep.NAME_RULES`) plus this module's container-lane
additions; the counter never opens, reads, copies or prints what it counts, and
the walk is bounded and does not follow directory symlinks. `mount_root` is a
required argument and must be an existing directory (§3b, finding p5-3). A
count of 0 without a scan does not establish a clean mount, so the record
also carries `mount_scanned` and `mount_entries_scanned`.

### Item 2 — `export/attestation_gate.py` (new): no RLVR labels without attested isolation

`require_attestation_for_labels(batch_manifest, attestation, *,
contamination_mode, config=None)` refuses a batch claiming RLVR labels when its
isolation record is missing, unsealed, tampered with, tier C or D, uses
contamination mode `observe` or `off` (or a mode different from the attested
one), or records a credential-shaped file in the mount. Every refusal is a typed
`AttestationRefusal` with a stable `code` from the closed `REFUSAL_CODES`
vocabulary. The caller prints `ERROR [code]` and exits 2, consistent with other
fail-closed harness refusals.

The setting is scoped: `release.require_attestation` (default `True`) can relax
only a batch that claims no labels. A label-claiming batch cannot skip the gate.
Under the default, a batch claiming no labels must still record a valid sealed
attestation, but is not tier-gated. This allows the `dev-only` laptop lane to
freeze an unlabelled release, as specified by Table 9's rollback row.

`batch_claims_rlvr_labels` reads a `ReleaseManifest`, a mapping or any object
with the same public fields, in the order: an explicit `claims_rlvr_labels`
boolean, then recorded EL/T variant acceptance or a shipped RLVR variant, then
the corpus profile. An unrecognized shape is treated as claiming labels, so a
new manifest kind requires an attestation.

The gate defines its own literals for the two corpus-profile names instead of
importing `export/release.py`. This allows `freeze_release` to call the gate
without an import cycle. `test_corpus_profile_constants_match_export_release`
pins the literals to the actual constants.

### Item 3 — `runtime/parity.py` and `tools/parity_sample.py` (new): the four-component battery

The battery measures how often the real stack disagrees on artifacts accepted
by the local DuckDB comparator.

1. Differential replay of a stratified sample through `terraform validate
   -json` (format 1.0, parsed strictly: unknown keys, duplicate keys, a wrong
   `format_version` and an oversized payload are refusals) and the `runtime
   verify-stage1` / `verify-stage2` / `verify-end-to-end` verbs per destination.
   Both runners are injected: this module builds argv and interprets receipts,
   and never starts a process, opens a socket or reads a credential. The
   credential enters argv as a path, and `--curator-details` is never passed, so
   private expected values cannot reach a parity report.
2. Proxy-rule mutation. `ComparatorRules` re-expresses the mart comparator
   as switchable rules and each mutant drops exactly one. A mutant is killed
   when some case separates it from the comparator the acceptance lane is
   actually running. A comparator that is a mutant cannot kill itself, so the
   lane fails [Just et al., 2014].
3. Metamorphic checks, SQLancer-style TLP and NoREC [Segura et al., 2016],
   over the DuckDB comparator on the committed five-backend fixture. TLP asserts
   that partitioning a relation by a predicate's ternary logic
   (`p` / `NOT p` / `p IS NULL`) reproduces the unpartitioned relation as the
   comparator judges it; NoREC asserts a filtered count equals the unoptimized
   `(p) IS TRUE` count. Neither needs gold.
4. Sampled real-warehouse replay is item 1 with the destination runner bound to
   a real destination. The owner must provide that binding.

`parity_rate` publishes `reports/parity_<arm>.json`:
`pi_c = P(real disagrees | local accepted)`, one-sided Wilson upper bounds at
85 % and 95 %, the agreement lower bound against the 0.90 adoption threshold,
McNemar mid-p over the artifacts both arms judged, the named population set with
its frame digest, the stratification shortfalls against the Table 9 floors (20
locally accepted, 10 locally rejected, 4 per source pool) and the quarantine
list. Under C7, a real-runtime fault is `reward=None`, counted in
`runtime_faults`, and excluded from the denominator. A warehouse fault therefore
cannot create or hide a disagreement. A local-accept / real-reject finding
quarantines the task and does not change a training label.

This does not add another reward implementation. The reference verdict is
`verification/upstream_eval.compare_mart`, pinned equal to `ComparatorRules()`
at its defaults over randomized shapes, and `wilson_interval` is imported from
`review/metrology.py`.

`tools/parity_sample.py` is the offline driver. `sample` draws the deterministic
stratified sample. `argv` prints the exact `elt-taskgen runtime verify-*` command
line for each specimen so the owner can run it with an operator credential that
does not enter this process. `report --attestation sandbox_attestation.json`
publishes the sealed report.

### Item 4 — `runtime/model_copy.py` (new): the model copy and the harness-owned replay copy

A22 control 2 requires the model-facing copy to carry only the placeholder
credential file (every string value empty — the public runtime shape
`export/eltbench.assert_public_runtime_shape` enforces) and a `config.yaml`
byte-identical to the frozen public bundle's. The harness-owned replay copy is a
separate directory that is created only after the policy finishes:
`release_model()` gates `install_replay()`, and the replay copy is the one the
`runtime verify-*` verbs are meant to execute in (pointing the verbs at it is
pending hand-off C2; the CLI still uses its previous location). On the cloud
lane, an attempt-scoped principal and revocation hook are required. Cleanup
revokes the principal through that injectable hook; this module and its tests
make no cloud call. A sealed `CleanupReceipt` records the result. A failed
revocation raises an error that includes the receipt because the principal
remains valid beyond the attempt.

`install_task` was owned by the concurrent effort, so until the roadmap's
`inject_credentials=False` parameter exists (§4), this module calls the
sanctioned public installer with the harness sentinel `"placeholder"` — a value
`credential_sweep` itself judges a placeholder. No transient live value enters
the model tree. The module then restores `config.yaml` and the credential file
from the frozen public bundle. It verifies the result before returning the
directory and destroys the tree if verification fails.

`GraderLaneRunner` enforces that the certification lane replays a sealed
artifact without making model calls. The replay container may run only `dbt`,
`docker`, and `terraform`, not an interpreter or agent-shaped argv. A `docker`
argv must be a plain pinned-image run (§3b, finding p5-7 — the allowlist judged
`argv[0]` alone, and `docker` is an allowed program); `agent_process_count` is
re-derived from the forwarded argv rather than asserted to be 0. The module
imports nothing from `review.providers` and starts no process. It never reads,
formats, returns, or logs a value from a credential file; assertions cover only
shapes, names, and emptiness.

### Item 5 — the cross-track integration (this pass)

Phase 5 completed three integrations within its own files:

* One credential-name vocabulary. `runtime/attestation.is_credential_shaped`
  now reads `review/tools/credential_sweep.NAME_RULES` — the repository's single
  definition, which `runtime/model_copy.py` and the `runs/` sweep already use —
  and adds only container-lane names not needed by the sweep. The
  standalone list it replaced omitted `profiles.yml`, `*.tfstate`, `*.tfvars`,
  `.aws_credentials` and `.envrc` entirely, so a mount holding a live dbt profile
  or Terraform state counted 0 and the gate accepted the batch. The counter uses
  names only and does not open files. It is stricter than the sweep.
* Which mount is counted. `credential_files_in_mount` is counted over the mount
  used by the labelled run, not an attempt `task/` directory:
  the upstream format puts a `<destination>_credential.json` there by contract,
  and the name-only counter cannot tell the exporter's placeholder from a live
  file without opening it. Using the model copy would therefore refuse every
  batch. The required mount is now specified and pinned in `attest_sandbox`'s
  contract. Pointing at the wrong tree records a non-zero count and causes a
  refusal. Omitting the mount or naming a path that is not a directory produces
  `mount_root_missing`, not 0 (§3b, finding p5-3).
* A parity claim is sealed and attributed. `ParityReport` gained
  `report_digest` (the same blank-the-field seal the attestation and the cleanup
  receipt use, applied by `write_parity_report` on the way out) and
  `sandbox_attestation_digest`, stored by `parity_rate(..., attestation=...)`
  after the record's seal is verified. Arms replayed under different container
  runtimes are different measurements, and the parity report must be verifiable.

The runner-image `.env` pin also has a harness-side check:
`assert_candidate_tree_is_image_safe` refuses a candidate tree carrying a
`.env`-shaped file (the shared `dotenv` name rule) before it is mounted, and
`GraderLaneRunner` calls it once per tree. `.env` is an out-of-band
configuration channel into dbt and Terraform. It can redirect
`DBT_PROFILES_DIR`, name another target, or carry values the harness never wrote
without recording those changes in the sealed replay artifact. The image-side
rejection remains a hand-off in §4.

## 2. Tests

The five modules contain 97 offline tests:

| Module | Tests | Roadmap contract names it carries |
|---|---|---|
| `tests/test_sandbox_attestation.py` | 17 | `test_sandbox_attestation_is_secret_free_and_sealed`, `test_sandbox_attestation_records_no_credential_in_mount`, `test_isolation_preflight_fails_closed_on_old_runc`, `test_preflight_asserts_runc_floor`, `test_preflight_asserts_runsc_floor`, `test_macos_runs_are_dev_only_in_attestation` |
| `tests/test_attestation_gate.py` | 10 | `test_release_refuses_tier_c_and_d_labels`, `test_release_refuses_observe_mode_attestation` |
| `tests/test_runtime_parity.py` | 51 | `test_parity_report_treats_runtime_faults_as_none_not_disagreement`, `test_parity_claim_names_its_population_set`, `test_emulator_mutation_fails_the_acceptance_lane` (no vendor emulator is built or claimed anywhere; the "emulator mutation" is a weakened DuckDB comparator rule), `test_terraform_validate_json_format_1_0_parsed`, `test_python_hcl2_pinned_exactly` |
| `tests/test_runtime_model_copy.py` | 17 (1 skipped) | `test_install_task_without_injection_leaves_placeholder`, `test_attempt_cleanup_revokes_scoped_principal`, `test_grader_container_never_ran_agent_process`, `test_cloud_image_rejects_dotenv_in_candidate_tree` |
| `tests/test_zero_cloud_rollout.py` | 2 | `test_ordinary_rollout_path_makes_zero_cloud_calls` |

`test_cloud_image_rejects_dotenv_in_candidate_tree` is the roadmap's
`skipUnless the image` test. It is skipped in this checkout because
`runtime-images/` was owned by the concurrent effort and the pin had not landed.
It reads the committed image build context without building, pulling, or running
anything. It passes when the owner lands the pin. The same property is currently
covered by
`test_candidate_tree_dotenv_is_refused_before_any_mount`, the equivalent
harness-side test.

Cross-track pins added in this pass:
`test_credential_names_extend_the_repository_sweep`,
`test_the_attested_mount_is_never_an_attempt_task_directory`,
`test_candidate_tree_dotenv_is_refused_before_any_mount`,
`test_parity_report_is_sealed_and_names_the_isolation_it_measured`,
`test_report_names_the_attestation_the_owner_supplies`.

Existing `tests/test_runtime_*.py` and `tests/test_certification.py` pass;
`tools/prove_admission_integrity.py` still prints `ALL PROOFS HELD`.

## 3. Sanctioned changes, and what did NOT move

| Change | Why it is sanctioned | Pinned by |
|---|---|---|
| `runtime/attestation.is_credential_shaped` reads the shared `NAME_RULES` | One name vocabulary; the standalone list missed `profiles.yml`, `*.tfstate`, `*.tfvars`, `.envrc` | `test_credential_names_extend_the_repository_sweep` |
| `ParityReport` gains `report_digest` and `sandbox_attestation_digest`; `write_parity_report` seals | The report is evidence: tamper-evident like every other Phase 5 record, and attributed to the isolation that produced it. No report has been published yet, so `PARITY_REPORT_SCHEMA_VERSION` stays `"1.0"` | `test_parity_report_is_sealed_and_names_the_isolation_it_measured` |
| `parity_rate(..., attestation=...)`, `tools/parity_sample.py report --attestation` | The owner's real-destination run has an attestation; an unsealed or edited one REFUSES rather than publishing an unattributed claim | `test_report_names_the_attestation_the_owner_supplies` |
| `GraderLaneRunner(..., check_candidate_tree=True)` refuses a `.env`-bearing tree | The harness-side twin of the fenced image pin; nothing in production calls this class yet | `test_candidate_tree_dotenv_is_refused_before_any_mount` |
| `tests/test_docs_consistency.py` `CURRENT_DOCS` gains this document | A strictly stronger check: the hand-off list names subcommands, modules and pins, so it is held to today's vocabulary like the Phase 0 to 3 logs and cannot promise a subcommand that does not exist, or a vendor emulator that was not built | `test_docs_only_name_real_subcommands`, `test_current_docs_never_claim_a_vendor_emulator` |

No other interface changed. No fingerprint, admission record, transcript key,
certification matrix, or release manifest changed: `HARNESS_VERSION` remains
`"6"`, `ADMISSION_SCHEMA` stays 4, `CERTIFICATION_MATRIX_VERSION` stays `"11"`,
and no existing test was deleted, weakened, or re-pinned. Phase 5's modules were
not called from `cli.py`, `export/release.py`, or the training loop. Those call
sites remain hand-offs in §4.

## 3b. The 2026-09-04 repair pass (adversarial review findings)

Four adversarial reviews covered Phase 4 and Phase 5. Phase 4 findings are in
`bounded_agents_phase4.md` §4b. The following entries describe the Phase 5
defects and corrected behavior.

| # | What was wrong | What now holds | Pinned by |
|---|---|---|---|
| p5-0 | The runsc security floor (GHSA-4fj4-9m67-3mj3) passed VACUOUSLY whenever the version string did not parse. The floor check is two-valued — it reads an unparseable or missing `runsc --version` exactly like "gVisor is absent" — while the TIER is derived from the engine's `DefaultRuntime`, so a source build (`runsc version devel`) or a runsc binary off the preflight's PATH minted a **Tier A** attestation with an empty `toolchain['runsc']`. The exact hosts the floor exists to reject were the ones it admitted. | A third value: `runsc_version_observed` is a REQUIRED check that fails (`runsc_version_unobserved`) whenever the engine registers runsc and no version could be read, and the tier-A branch additionally requires `runsc_meets_floor`. A host with no gVisor at all is untouched: tier C, no failure. | `test_runsc_version_unobserved_is_a_failure_not_a_pass` |
| p5-1 | Setting the unvalidated env var `ELT_TASKGEN_SANDBOX_HOST_CLASS` demoted the docker-info, cgroup-v2 and cgroup-driver checks to advisory and raised the reported tier to B **before runtime inspection**, so `isolation_preflight` passed on a cgroup v1 host and on a host with no container runtime at all (`runtime: none`, `cgroup: ''`) — and the gate then admitted that record for RLVR labels. One env string caused an unisolated host to be recorded as label-bearing. | The declaration may excuse the ABSENCE of `docker info` and nothing else: an engine that answered and reported cgroup v1 is an observed cgroup v1, and a declaration with no observed runtime or cgroup behind it fails `declared_host_class_uncorroborated`. On the read side the gate refuses a tier B record whose runtime or cgroup evidence is empty (`attestation_tier_uncorroborated`). | `test_declared_host_class_cannot_demote_observed_isolation_evidence`, `test_gate_refuses_an_uncorroborated_tier_b_record` |
| p5-2 | `batch_claims_rlvr_labels` let a manifest's self-declared `claims_rlvr_labels: false` win OUTRIGHT, before `variant_acceptance`, `variants` or `corpus_profile` were consulted — so anyone who could write the release directory could add one key to `manifest.json` and carry a batch of RLVR variants past the tier, contamination-mode and credential-in-mount refusals entirely. | The signals are monotone in the fail-closed direction: an explicit `false` is honoured only when nothing else in the same manifest contradicts it, and a contradiction is `batch_manifest_invalid`. An explicit `true` still claims labels a profile alone would not. | the re-pinned `test_batch_claims_rlvr_labels_reads_manifests_and_fails_closed` |
| p5-3 | `attest_sandbox` defaulted `mount_root` to None and the counter answered 0 for a missing argument, a typo'd path and a non-directory alike — so A22 control 5 (`credential_files_in_mount == 0`) read as PROVEN without anything ever being scanned, and the sealed record could not tell that from a scan that found nothing. | `mount_root` is a REQUIRED keyword and must be an existing directory (`mount_root_missing`); the record carries `mount_scanned` and `mount_entries_scanned`, and the gate refuses a label-claiming batch whose attestation records no scan (`mount_not_scanned`). | `test_attest_sandbox_requires_a_mount_to_scan`, `test_gate_refuses_an_attestation_that_scanned_no_mount` |
| p5-4 | The Table 9 sizing floors were computed over the DRAWN sample's locally-accepted rows and never read `measured`, while the denominator was built from `judged` only — so runtime faults left the denominator without registering as a shortfall, and 12 real measurements against a floor of 20 reported `shortfalls: ()` and `adopted: True`. | `parity_rate` adds `denominator_below_minimum:<arm>:<judged>/<floor>` over the MEASURED conditioning set, and `adopted` depends on it; `stratification_shortfalls` keeps its drawn-sample meaning for the sampler's pre-replay use. | `test_sizing_floor_is_measured_after_runtime_faults` |
| p5-5 | `PopulationSet.artifact_ids`/`size` enumerated every locally accepted artifact, faults included, while `pi_c` and the agreement bound were computed over the measured subset — so a reader quoting `report.population_set` beside `report.agreement_lower`, both top-level sealed fields, attributed a 12-artifact bound to a 20-artifact frame. `population_set_name` was unvalidated free text that REPLACED the derived description. | The named set is the MEASURED frame, the faulted ids are carried in `unmeasured_artifact_ids`, a model validator refuses a report whose named set is not exactly its denominator (and whose unmeasured members do not account for every fault), and the caller's name is an ANNOTATION appended to the derived description. | `test_population_set_names_the_measured_frame_only`, `test_population_set_name_is_an_annotation_not_a_replacement` |
| p5-6 | The sealed `CleanupReceipt` hardcoded `live_credential_files=0`, and `cleanup()` neither checked that `release_model()` had run nor looked at the replay tree — so a `remove_replay=False` cleanup on the cloud lane sealed a receipt asserting a verification that never happened while the replay tree with LIVE values was still on disk. | The count is MEASURED at receipt time over what survived (the model copy's harness surface plus the replay tree when it survives), and the receipt records `model_released`. | `test_cleanup_receipt_counts_the_credentials_that_survived` |
| p5-7 | `GraderLaneRunner` applied its program allowlist to `argv[0]` alone, and `docker` is on the allowlist — so `docker run --entrypoint python`, `docker run img python3 /agent/loop.py`, `docker run -v /:/host img sh -c ...` and `docker exec c bash -lc ...` all ran, while `agent_process_count` was a property returning 0 unconditionally. The module's own docstring claimed the lane runs "never an interpreter". | `INTERPRETER_ARGV_TOKENS` is matched over the whole argv as exact tokens, `assert_docker_argv_is_replay_shaped` refuses a non-`run` subcommand and the flags that turn a pinned-image run into arbitrary execution (`--entrypoint`, `--privileged`, `--cap-add`, `--security-opt`, `--pid`, `--userns`, `--device`, `-v`/`--volume`/`--mount`), and `agent_process_count` is re-derived from the argv that were FORWARDED. | `test_grader_lane_refuses_docker_interpreter_escapes` |
| p5-8 | `package_digest`, `tool_manifest_sha256`, `verifier_code_sha256` and `diagnostics_version` were passed through as arbitrary caller strings. `assert_attestation_is_secret_free` is a keyword denylist and an opaque high-entropy token (an access key, a JWT, a bot token) carries none of those keywords, so four fields of the "closed roster" were an open channel for secret material into a sealed, published record. | Every digest field is shape-checked against `_SHA256` (empty still allowed) and `diagnostics_version` against the projection module's dotted-version shape. A closed SHAPE is the structural control the keyword denylist cannot be. | `test_digest_fields_refuse_free_text` |
| p5-9 | The zero-cloud guard rebound only the `socket` module's Python wrappers, leaving `_socket` — a plain Python-level import — fully usable, so the guard reported itself armed while `_socket.socket(...)` opened a real socket. The ordinary-path leg of the test also ran the parent rollout with no socket patch at all, though the module docstring said otherwise. | The guard patches `_socket` FIRST and adds `socketpair` / `create_server`; the `_PROBE` asserts the `_socket` refusal by name; and `_amputated_parent_sockets()` wraps BOTH legs, so the `score_in_child` plumbing is measured under the amputation the docstring claims. | `test_ordinary_rollout_path_makes_zero_cloud_calls` |
| p5-10 | `SandboxAttestation` carried no run id and no timestamp, and the gate never related the record to the batch it was admitting — so one Tier A record replayed indefinitely for every future release, including on a host since decommissioned. `CertificationAttestation`, the convention it cites, binds `certification_id`, `release_id`, `task_id`, `started_at` and `completed_at`. | `attested_at` (ISO-8601 UTC) and `run_id` join the roster; `require_attestation_for_labels(..., run_id=, now=, max_age_s=)` refuses a foreign run (`attestation_run_mismatch`) and a record older than `MAX_ATTESTATION_AGE_S` (30 days) or carrying no timestamp (`attestation_stale`). | `test_attestation_records_when_and_for_which_run`, `test_gate_binds_the_attestation_to_its_run_and_refuses_a_stale_one` |

`SANDBOX_ATTESTATION_SCHEMA_VERSION` changed from `"1.0"` to `"1.1"` with the
four new fields. The version participates in the digest, so a 1.0 record cannot
be treated as a record that identifies its time, run, and mount scan. No
attestation had been created because this checkout was a tier D `dev-only`
laptop, so the change invalidated nothing. `PARITY_REPORT_SCHEMA_VERSION`
remained `"1.0"` because no report had been published.

Hand-off C1 in §4.3 calls `attest_sandbox(..., mount_root=<the scored
workspace>)`. It must also pass `run_id=<the release id>`. The `freeze_release`
call to `require_attestation_for_labels` in R1 must also pass `run_id`, so the
gate can bind the record to the batch.

## 4. Owner hand-off to the runtime/export effort

Each row specifies a pending change to a file that Phase 5 did not own. Line
anchors were read on 2026-09-04.

### 4.1 `export/release.py` — the gate's call sites

| # | Function | Exact change | Test that would pin it |
|---|---|---|---|
| R1 | `freeze_release` (`release.py:1371`) | Add `attestation: SandboxAttestation \| None = None`. Immediately before `manifest_path = staging / "release_manifest.json"` (`release.py:1668`) call `export.attestation_gate.require_attestation_for_labels(manifest, attestation, contamination_mode=verification.contamination.enforcement().value)`. Let `AttestationRefusal` propagate; the CLI prints `ERROR [{exc.code}]` and returns 2, the `ToolchainPinError` pattern already at `cli.py:3701-3704`. Write the sealed record beside the manifest as `sandbox_attestation.json` so `checksums.sha256` covers it. | extend `test_release_refuses_tier_c_and_d_labels` and `test_release_refuses_observe_mode_attestation` to drive `freeze_release` over the `TestFreezeRelease` harness (`tests/test_export.py:1399`) |
| R2 | `verify_release` (`release.py:2062`) | Load `sandbox_attestation.json`, call `attestation_gate.recompute_attestation_digest(record)` and compare it to `record.attestation_digest`. Report a `ReleaseVerification` FAILURE; never raise (this function fails closed by reporting). | `test_verify_release_recomputes_the_sandbox_attestation_digest` |
| R3 | `ReleaseManifest` (`release.py:322`) | Add `sandbox_attestation_digest: str = ""` (empty = legacy / pre-Phase-5), written by `freeze_release` from `attestation.attestation_digest`, so a frozen manifest names the isolation it shipped under. Note that `freeze_release` returns `ReleaseManifest`, not the roadmap interface block's `ReleaseResult`. | `test_release_manifest_records_the_attestation_digest` |

### 4.2 `runtime_matrix.py` — placement and the matrix

| # | Function | Exact change | Test that would pin it |
|---|---|---|---|
| M1 | module surface | Table 9 places `SandboxAttestation` / `isolation_preflight` / `attest_sandbox` in `runtime_matrix.py`; they shipped in `runtime/attestation.py` because `runtime_matrix.py` is fenced. For the roadmap's literal placement add `from elt_taskgen.runtime.attestation import SandboxAttestation, attest_sandbox, isolation_preflight  # re-export` plus the `__all__` entries. This is safe for `runtime_matrix`'s dependency-light style: `runtime/attestation.py` imports only `elt_taskgen.models` at module scope and everything else is lazy. | `test_runtime_matrix_reexports_the_sandbox_attestation_surface` |
| M2 | `_certification_matrix_keys()` and `certification_matrix()` (`runtime_matrix.py:258`, `:550`) | Add `sandbox_attestation_schema_version` (value `runtime.attestation.SANDBOX_ATTESTATION_SCHEMA_VERSION`) and, if the isolation floors are to be identity-bearing, `sandbox_runc_floor` / `sandbox_runsc_floor`; bump `CERTIFICATION_MATRIX_VERSION` `"11"` to `"12"` and freeze the v11 recipe as a `_V11_*` block the way `_V10_*` preserves v10. **Cost: this rotates every certification identity**, so it is a deliberate owner action, ideally bundled with the pin changes in §4.4. | `test_certification_matrix_moves_when_the_sandbox_floors_move` |

### 4.3 `runtime/install.py` and `cli.py`

| # | Function | Exact change | Test that would pin it |
|---|---|---|---|
| I1 | `install_task` (`install.py:464`) | Add `inject_credentials: bool = True`. With `False`, install the public bundle and leave `config.yaml` and `<destination>_credential.json` exactly as the frozen public copy has them (placeholders), skipping every injection branch while keeping the symlink refusal, the cross-attempt state stripping, the owner-only hardening and the single publishing rename. `runtime/model_copy.install_task_for_model` then drops its sentinel-and-restore workaround and calls `install_task(..., inject_credentials=False)` directly. | `test_install_task_without_injection_leaves_placeholder` (exists; re-point it at the real parameter) |
| C1 | `cli.py`, every label-bearing verb's run start | Call `runtime.attestation.isolation_preflight()`, then `attest_sandbox(pinned=providers.sandbox_pin(), preflight=result, mount_root=<the scored workspace>)`. On `SandboxAttestationError` print `ERROR [{exc.code}]: {exc}` and return 2 with nothing measured, mirroring the toolchain-pin refusal at `cli.py:3698-3704`. | `test_cli_exits_two_on_a_sandbox_pin_mismatch` |
| C2 | `cli.py` `cmd_runtime_verify_stage1` / `_stage2` / `_end_to_end` (`cli.py:6366-6374`; the roadmap's `:5137` anchor is stale) | Execute in the harness-owned REPLAY copy (`model_copy.AttemptCopies.replay_dir`), never in the model-facing `task/` copy, and revoke the attempt-scoped principal at cleanup on the cloud lane. | `test_runtime_verify_runs_in_the_replay_copy` |

### 4.4 `runtime-images/`, `pyproject.toml` and the preflight pins

The following Table 9 pin states were verified on 2026-09-04:

| # | Pin | State today | Remaining change | Test that would pin it |
|---|---|---|---|---|
| P1 | Terraform `1.15.8` | **Landed**: `runtime-images/terraform/Dockerfile` is `hashicorp/terraform:1.15.8@sha256:7ae5132...` | none | `test_pins_are_declared_with_their_sources` |
| P2 | `filesystem_mirror` provider source | **Landed**: `runtime-images/terraform/terraform.rc` mirrors `registry.terraform.io/airbytehq/airbyte` from `/opt/terraform/providers`, `disable_checkpoint = true` | none | — |
| P3 | `terraform validate -json` with `-backend=false`, no network | **Not landed**: nothing runs `terraform validate` today (`runtime/execution.py:1373` runs `init -input=false` then `apply`). Add the validate step with `-json -backend=false` on the `none` network lane, and parse it with `runtime.parity.parse_terraform_validate_json` (strict, `format_version` 1.0) rather than a second parser. | `test_terraform_validate_json_format_1_0_parsed` (exists, over the parser), plus `test_validate_runs_with_backend_false_and_no_network` |
| P4 | dbt-core `1.12.0` | **Landed**: `runtime-images/dbt/pyproject.toml` pins `dbt-core==1.12.0` (snowflake 1.12.0, databricks 1.12.4, redshift 1.11.1) | none | `runtime_matrix` matrix keys already carry `dbt_core_version` |
| P5 | dbt images set `DBT_ENGINE_PROFILES_DIR` and `DBT_PROFILES_DIR` explicitly | **Not landed**: only `training/dbt_runner.py:1625` sets `DBT_PROFILES_DIR`, and no image sets either | Set both in each `runtime-images/dbt-*/Dockerfile` (or the shared entrypoint) so an unset variable can never fall back to `$HOME/.dbt` | `test_cloud_image_rejects_dotenv_in_candidate_tree` (already asserts both names once it un-skips) |
| P6 | images reject `.env` in candidate trees | **Not landed** | Refuse at image entry when the mounted candidate tree carries a `.env`-shaped file. The harness-side twin (`model_copy.assert_candidate_tree_is_image_safe`) already refuses before the mount and uses the shared `dotenv` name rule; match that set. | `test_cloud_image_rejects_dotenv_in_candidate_tree` |
| P7 | images strip `DBT_` and `DBT_ENGINE_` prefixes from the inherited environment | **Not landed** | Strip both prefixes at image entry before the two explicit variables from P5 are set, so an inherited `DBT_TARGET`/`DBT_PROFILES_DIR` cannot redirect the build | `test_dbt_image_strips_inherited_dbt_prefixes` |
| P8 | abctl `v0.30.4` | **Landed**: `runtime_matrix.AIRBYTE_ABCTL_VERSION = "v0.30.4"` | none | `certification_matrix` carries `airbyte_abctl` |
| P9 | Airbyte chart `2.0.19` | **DISAGREES**: `runtime_matrix.AIRBYTE_CHART_VERSION = "2.2.0"` | An owner decision, not a code edit anyone should make silently: Table 9 says 2.0.19, the checkout says 2.2.0, and the matrix key `airbyte_chart` means changing it rotates every certification identity (see M2). Decide, then land the decision with the M2 bump in one commit. | `certification_matrix` carries `airbyte_chart`; add `test_airbyte_chart_pin_matches_the_certified_matrix` |
| P10 | `python-hcl2==7.3.1` | **Not landed**: `pyproject.toml:24` declares the RANGE `python-hcl2>=7,<8` | Narrow to `==7.3.1`. 7.x and 8.x disagree about the parsed dict shape, so a battery reading Terraform through 8.x while claiming the 7.x pin measures a different program than it names. | `test_python_hcl2_pinned_exactly` (exists; it holds the pin against the INSTALLED version today and will hold it against the declaration once narrowed) |
| P11 | `runtime/process.py` cloud lane | **Landed**: `DOCKER_LANE_PROXY_BRIDGE` exists and `model_copy.docker_lane_for(LANE_CLOUD)` selects it; the DuckDB lane stays `DOCKER_LANE_NONE` | Bind the proxy bridge so the Airbyte control plane is its only reachable endpoint (operator network config, not code) | `test_docker_runner_network_none_for_local_tools` (exists) |

### 4.5 Docs inside the fence

| # | File | Exact change | Test that would pin it |
|---|---|---|---|
| D1 | `docs/EXECUTION_MODEL.md` | The status paragraph's third sentence, "No cloud certification attestation is currently recorded in this checkout", is a TIME-BOUND factual pin. It and `tests/test_docs_consistency.py::test_execution_model_status_paragraph_is_complete` (the roadmap's `:771` anchor has moved) must be rewritten in the SAME commit as the first real tier A or B attestation — that is the forcing function, and it is the reason `tests/test_docs_consistency.py` is editable in this pass while `docs/EXECUTION_MODEL.md` is not. | `test_execution_model_status_paragraph_is_complete` |
| D2 | `docs/WAREHOUSE_CONNECTORS.md` | The runbook already documents per-attempt credential injection into a fresh copy and the per-destination isolation row. Add the two Phase 5 facts it does not carry: (a) the model-facing copy holds only the PLACEHOLDER credential file while live values go into a separate harness-owned replay copy created after the model is gone, and (b) the attempt-scoped principal is revoked at cleanup on the cloud lane, with the sealed receipt as the evidence. State the sandbox tier a certification run must be on (A or B). | `test_warehouse_runbook_documents_the_replay_copy_and_revocation` |

### 4.6 `config/agents.yaml` and `review/providers.py`

`release.require_attestation` is read from a top-level `release:` block of
`config/agents.yaml` and defaults to `True` when absent, so this checkout refuses
unattested labels. Making the default explicit requires changes to two files:
`tests/test_providers.py::test_default_routing_doc_matches_agents_yaml` asserts
`providers.DEFAULT_ROUTING_DOC == yaml.safe_load(config/agents.yaml)` whole, so
adding the block to the file without adding it to `DEFAULT_ROUTING_DOC` fails the
suite. Add both in one commit. This does not change the fingerprint, which hashes
named blocks rather than the document.

Pinned by `test_require_attestation_setting_matches_the_committed_config`
alongside the existing `test_default_is_true_and_config_is_read` and
`test_default_routing_doc_matches_agents_yaml`.

### 4.7 The observed attestation on label-bearing records

Table 9's admission row requires the observed attestation on every label-bearing
ledger row and every `WorkspaceScoreResult`.

| # | File | Exact change | Test that would pin it |
|---|---|---|---|
| A1 | `training/models.py` `WorkspaceScoreResult` (`:217`) | Add `sandbox_attestation_digest: str = ""`, set from the attestation the runner holds. | `test_every_label_bearing_row_stamps_the_attestation_digest` |
| A2 | `engine.py` ledger row writer | Add the same column, defaulting empty so every historical row stays valid; the ledger is append-only. | same test |

## 5. Owner actions (they need a real host or a real warehouse)

1. Create the first pinned cloud attestation on a tier A or B host, with the
   preflight hash inside, recorded beside the release manifest as
   `sandbox_attestation.json`, in the same commit as D1. Offline work cannot
   produce this record: `attest_sandbox` refuses a host whose
   preflight did not pass, and this checkout is a laptop (tier D, `dev-only`).
2. Re-pin `metrology.sandbox` to that host. The admission fingerprint hashes the
   pin (R-F), so changing it re-keys transcripts, makes the admission stale, and
   requires a re-earn. Combine it with the Phase 4 harness-6 re-earn.
3. Run the parity battery on real destinations against the sealed artifacts of
   the Phase 2 pilots P2 and P3, per arm and per destination:
   `tools/parity_sample.py sample`, then the printed `runtime verify-*` command
   lines with an operator credential that never enters this process, then
   `report --attestation`. The definition of done is an agreement lower bound of
   at least 0.90 per arm on every destination; 0 disagreements in 20 gives a
   one-sided 85 % Wilson upper bound of 0.051 (95 %: 0.119), and a 5 % bound at
   95 % needs about 52 artifacts one-sided. Any local-accept / real-reject on a
   state-changing operation is filed as a runtime-parity task-pool defect that
   quarantines the task and does not change a label.
4. Complete the image, chart, and dependency pins in §4.4: P3, P5, P6, P7, P10,
   and the P9 chart-version decision with its M2 matrix bump.
5. Complete the pending owner actions from the earlier phases:
   - Phase 0: the credential rotation and purge under
     `runs/runtime_canary_20260901/databricks/live/`, after which
     `test_runs_tree_carries_no_credential_shaped_files` runs ungated; the live
     fixture re-record; the legacy Snowflake password decision.
   - Phase 1: pilots P1 and P6, and the metered live smoke per backend.
   - Phase 2: pilots P2 and P3 (their sealed artifacts are this phase's input),
     and the deferred 2.c interactive prerequisites, which need the same tier A/B
     host as item 1.
   - Phase 3: pilot P4, and the `measured_match_bit` / `attack_enabled`
     decisions.
   - Phase 4: the first fresh-live re-earn under harness `"6"`
     (`elt-taskgen metrology --workspace council --budget-per-task 60`), the
     seat flip after P4 and P5 adopt, and pilot P5.

## 6. Engineering follow-ups

- `attest_sandbox` defaults `runtime_json_sha256` and `oci_config_sha256` to
  `""` because nothing offline can observe them; the runtime stage runner must
  supply them, which is part of C1/C2 in §4.3.
- `PARITY_REPORT_SCHEMA_VERSION` stays `"1.0"` because no report has been
  published. The first real publication is the moment to freeze it.
- The parity battery's destination runner is a one-function callback interface
  (`DestinationRunner = Callable[[ReplaySpecimen], ReplayOutcome]`). Binding it
  to `runtime/evaluation.py`'s collectors is the next engineering change after
  item 1.
