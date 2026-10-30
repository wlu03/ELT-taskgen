# Public model specification prompt comparison

ELT-Bench is a comparison reference in this report only. No benchmark name or task examples are included in the model prompts.

Five prompts were compared on four development tasks; the selected prompt was frozen before four held-out tasks. The tasks cover film studios, advertising and video activity, with different aggregation/selection rules. The reference style distribution covers 100 local benchmark tasks and 202 models.

| Prompt | Valid schema | No semantic issues (LLM review) | Mean style / 5 | Prose words |
|---|---:|---:|---:|---:|
| brief | 4/4 | 2/4 | 4.75 | 492 |
| contract | 4/4 | 4/4 | 4.38 | 618 |
| business | 4/4 | 4/4 | 4.50 | 654 |
| concise | 4/4 | 3/4 | 4.75 | 427 |
| balanced | 4/4 | 4/4 | 4.75 | 464 |

The four development baselines contain 512 prose words. Semantic fidelity takes priority over length or style scores.

The brief rewrite lost required rules. The detailed variants preserved rules but expanded the prose. The concise variant blurred related-record existence with a nonmissing ID. The balanced variant explicitly preserves that distinction without explaining joins.

Selected prompt: **balanced**. [Read the instructions](winning_prompt.txt).

Held-out results: 3/4 valid documents on the first author attempt; 4/4 after recorded empty-response retries; 4/4 with no semantic issues reported; mean style 4.62/5; 500 words versus 553 in the baselines.

[Actual generated example](outputs/fan_out_rollup/balanced/data_model.yaml) · [Baseline](cases/fan_out_rollup/baseline.json)

| SQL recovery arm | Tasks passing all three populations |
|---|---:|
| baseline | 8/8 |
| balanced | 8/8 |

SQL attempts use the same model, schemas, scoring and budgets. Each attempt is checked against primary, resampled and counterfactual gold. This tests output recoverability; it is not the full ELT pipeline benchmark.

Reference style: median of per-model median column lengths is 12.5 words. Length alone is not an accuracy score; tasks with more required qualifiers need longer definitions.

Limitations: one model (moonshotai/kimi-k3), four dev and four held-out tasks, one completed rewrite and one SQL attempt per arm/task (author failures/retries reported separately). Author, semantic reviewer and solver use separate calls to the same model, so LLM review is not independent ground truth. The four studio fixtures use compiled reference gold; the other four reuse the earlier independent-oracle pilot's gold. These results do not establish general accuracy on ELT-Bench.

The complete campaign's raw prompts, responses, inputs and transcripts remain in the local `testing/results/model-spec-prompts-business/` directory. This checked-in evidence snapshot contains the measured summaries, generated YAML, baseline definitions and per-task results. One empty review response and one held-out author response were retried; the author failure remains in the first-attempt count. The interrupted, superseded campaign is excluded.

Reproduction requires the original frozen case bank (`--case-bank /path/to/dependent-pilot/cases`) and the local reference model directory (`--bench-root /path/to/ELT-Bench/elt-bench/snowflake`). Prepare with `--phase prepare`, then collect missing responses with `--phase dev` and `--phase holdout --winner balanced --solve`. A complete existing campaign can rebuild its local report with `--phase report`. Live calls are metered with an $8 cap. Source data and credentials are not part of this evidence snapshot.

The production export replay confirms that all eight measured documents pass the author interface and are exported unchanged. Targeted offline checks pass. The previous live transcript/admission recordings need refreshing before release; no full benchmark or release-readiness claim is made.
