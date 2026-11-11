# Failure driven evolution cycle

This workflow turns reviewed retrieval failures into a bounded candidate decision. It does not update runtime settings or the knowledge base.

## Decision rules

The cycle reads a regression report, a separate challenge report, the independent test retrieval report, the matching answer quality report, threshold configuration, and both development-set manifests. A candidate is eligible for operator review only when:

1. Both development runs are complete and use the same code revision and knowledge snapshot.
2. Recall@5 improves over baseline on both the regression and challenge sets, and MRR does not regress on either set.
3. The independent-test release gate returns `PASS` for that candidate.
4. `data/ch11/failure_development_review.csv` contains an approved review and reviewer ID for every failure family represented in both development sets. Review the whole category, including its listed examples and expected labels, instead of approving each query separately.

Missing or inconsistent reports produce `INCONCLUSIVE`. A candidate that does not improve the target sets or fails the release gate is rejected. Even an eligible candidate is only a recommendation; application to runtime configuration remains an operator action.

## Run it

First run `scripts/eval_candidates.py` on the regression and challenge datasets, with the same knowledge version and code revision. Then combine those reports with the independent-set and answer-quality reports:

```powershell
$env:PYTHONPATH='.'
$env:NO_PROXY='localhost,127.0.0.1,::1,api.siliconflow.cn,api.deepseek.com'
$env:no_proxy=$env:NO_PROXY
uv run --python 3.12 python scripts/evolution_cycle.py `
  --regression-report data/ch11/reports/eval_evolution_cycle1_failure_regression_current.json `
  --challenge-report data/ch11/reports/eval_evolution_cycle1_failure_challenge_current.json `
  --independent-report data/ch11/reports/eval_candidates_independent_test.json `
  --answer-report data/ch11/reports/eval_answers_independent_candidates.json `
  --thresholds data/ch11/release_thresholds_currentcode_draft.json `
  --regression-manifest data/ch11/failure_regression_manifest.json `
  --challenge-manifest data/ch11/failure_challenge_manifest.json `
  --development-review data/ch11/failure_development_review.csv `
  --out data/ch11/reports/evolution_cycle1_automated_decision.json
```

The `NO_PROXY` entries keep local Milvus traffic and configured model APIs out of the local desktop proxy. Adapt the model API hosts if `.env` points to different providers.

## Propose from live reviewed cases

`GET /api/diagnosis/cases/evolution-proposals?min_cases=3` aggregates only cases whose diagnosis status is `confirmed` and whose human failure label is known. It emits bounded recall-depth profiles for `retrieval_miss` and rerank-depth profiles for `rerank_error`; other categories are routed to answer, tool, routing, or knowledge review. Fewer than three confirmed cases in a category produce no retrieval candidate. The endpoint is read-only and never applies a proposal.

At the current smoke check the endpoint returned HTTP 200 and zero confirmed cases. Real customer-feedback-driven proposals will appear after reviewed cases accumulate; the offline cycle below was exercised with the existing evaluation failure sets.

## Batch knowledge-gap consolidation

The existing low-confidence-question flywheel now processes at most 10 cases per normalization call. A first model role proposes canonical questions and candidate matches; a second review role runs only when cases would be merged with each other or with an existing review item. Both roles currently use the configured chat model in separate calls. The reviewer checks that intent and applicable policy match, rather than relying on shared keywords. If that check rejects a merge, the cases remain separate. If either structured response is incomplete or invalid, the batch stays unmatched for a later retry. Versioned logical tasks and per-request `invocation_id` records are persisted in `flywheel_tasks` and `flywheel_invocations`; a completed normalization result can be reused if merge review or the database commit fails. The validated queue writes and source links are committed atomically per batch, so a retry does not duplicate queue rows or occurrence counts. Invocation records keep response hashes and error types, not raw prompts or responses. Persisted structured task results expire after 30 days; invocation hashes and task metadata remain for audit.

This reduces repeated model calls while preserving the current human approval boundary: outputs are written to `review_queue`, original cases remain linked for inspection, and nothing is written directly to the knowledge index. The scheduled `make flywheel` run prints normalization and merge-review call counts for cost tracking. On an existing local database, run `make migrate-self-evolution` once before running the updated pipeline; this applies the ch12 through ch15 schema changes.

Diagnosis capture retains the legacy case fields for existing screens and now also stores independent diagnostic scopes and evidence-linked fault items. Explicit tool execution failures and deterministic evidence-gate refusals can coexist in one case; user dissatisfaction by itself remains inconclusive. Human changes append review events and recompute the case-level diagnosis status from subtask state.

## Topic classification and diagnosis proposals

`make diagnose-cases` now runs a separate batch workflow over unresolved diagnosis cases. It classifies questions through the existing RoBERTa service at `:8110`, then asks the chat model for per-case, evidence-referenced fault proposals. A batch contains at most 10 cases. The classifier service exposes a SHA-256 version over its ONNX model, tokenizer, and threshold artifacts; that version and the analysis prompt/schema version form the diagnostic scope, so changing either creates a new logical task. The model output is checked for exact case coverage and evidence references before each case task and proposed fault item is persisted. Model proposals remain unconfirmed; human review and deterministic rules continue to own confirmation. `make diagnose-cases LIMIT=50 BATCH_SIZE=10` requires the ch12 through ch16 migrations, a working MySQL database, classifier service, and chat API. This code path has not yet had live-service acceptance.

## Cycle result and points-withdrawal repair

The earlier post-review retrieval rerun (before correcting the E40 answer labels and prompt boundary) reported different Recall@5 values. Use the current same-revision results in “Existing challenge-set rerun after the policy and evaluation repairs” below; those reports supersede the earlier retrieval numbers.

The pre-fix independent answer report failed for `FTC-D-005`: the answer asserted that points could not be withdrawn as cash, while the approved expected handling required saying that withdrawal is undocumented and must be verified. The KB and RAG prompt now distinguish order deduction from cash withdrawal. The KB says that 100 points deduct 1 yuan from an order, and that this does not establish whether points may be withdrawn; the prompt requires answering known parts while explicitly qualifying unknown policy.

A live, post-fix one-case RAG evaluation on `FTC-D-005` completed for `baseline`, `recall_20`, and `rerank_5`. All three were judged handling-compliant, with zero unsupported claims. This is a targeted regression check. Per the current project decision, continue using the existing regression and challenge sets; do not create another independent dataset. Because those sets are used during development, report them as development/acceptance evidence for this project, not as an untouched independent release test. The earlier `NO_CANDIDATE` decision report reflects the pre-fix answer report and must not be treated as the current post-fix quality result. No runtime retrieval configuration was changed.

A post-fix 50-question diagnostic rerun also reported 10/10 handling-compliant D_absent answers and zero unsupported claims for all three candidates; answer coverage was 1.00 for `baseline` and `recall_20` (0.99 for `rerank_5`), and faithfulness was 1.00 for all candidates. `recall_20` and baseline had the same independent-set Recall@5/MRR (0.9917/0.8852), while `recall_20` reduced false-refusal rate from 0.125 to 0.10. The release gate correctly returns `INCONCLUSIVE`: this previously approved set has now been evaluated and used to guide a repair, and the old thresholds are bound to a different code/knowledge snapshot. These numbers are diagnostic only, not an adoption decision.

The six category rows in `data/ch11/failure_development_review.csv` are approved by reviewer `wxz`; the development manifests remain development-only and are not eligible as final test data.

## Existing challenge-set rerun after the policy and evaluation repairs

The existing 18-question regression set and 30-question challenge set were used; no new questions or separate dataset were created. Five E40 rows had an answer-label mismatch: the questions asked whether power restoration requires re-pairing, while `expect_points` required the general first-time QR setup instruction. The KB documents QR setup for first activation only. We corrected those rows so answer coverage measures the documented outage fact, while `expected_handling` checks that the answer does not infer re-pairing from configuration persistence. The row count and retrieval section expectations are unchanged. Dataset hashes and report hashes are recorded in the manifests.

The RAG answer prompt now explicitly distinguishes saved configuration from Wi-Fi pairing state. On the same code revision and Milvus snapshot, `recall_20` beat baseline on retrieval in both sets: regression Recall@5/MRR was 0.6944/0.5377 vs 0.6204/0.5336; challenge was 0.7167/0.5528 vs 0.6722/0.5503. False-refusal rates were unchanged (0.1667 regression, 0.1333 challenge); `rerank_5` did not improve retrieval.

The challenge answer run reports 1.000 answer coverage and 1.000 faithfulness for all candidates. On the five E40 handling cases, every candidate now scores 5/5 compliant with zero unsupported claims. Before the prompt fix, the same handling judge found unsupported claims in 2/5 baseline answers, 1/5 `recall_20` answers, and 0/5 `rerank_5` answers. That establishes a concrete benefit from the boundary rule. `recall_20` also has the best retrieval metrics while matching baseline answer quality, so it is the strongest candidate on these existing sets. The challenge contains only E_multi questions, so it does not evaluate D_absent refusal quality or other buckets. Runtime retrieval configuration remains unchanged; the report supports adopting `recall_20` for this project, but does not claim independent release validation.

Reports for this rerun:

- `data/ch11/reports/eval_evolution_cycle1_failure_regression_postfix_20261023.json`
- `data/ch11/reports/eval_evolution_cycle1_failure_challenge_postfix_20261023.json`
- `data/ch11/reports/eval_answers_failure_challenge_postfix_20261023.json`

Reports:

- `data/ch11/reports/eval_evolution_cycle1_failure_regression_postreview_20261023.json`
- `data/ch11/reports/eval_evolution_cycle1_failure_challenge_postreview_20261023.json`
- `data/ch11/reports/cash_withdrawal_postfix_smoke.json`
- `data/ch11/reports/eval_answers_postfix_diagnostic_20261023.json`
- `data/ch11/reports/eval_candidates_postfix_diagnostic_20261023.json`
- `data/ch11/reports/release_gate_postfix_diagnostic_20261023.json` (diagnostic gate; expected `INCONCLUSIVE`)
- `data/ch11/reports/evolution_cycle1_automated_decision_postreview_20261023.json` (pre-fix answer gate)
- `data/ch11/failure_development_review.csv`


## Self-evolution implementation additions

The code now includes database-backed knowledge snapshots, immutable system-config versions and release records, plus a Milvus `knowledge_active` alias used for switching a prepared snapshot. `make migrate-self-evolution` applies the ch12 through ch16 schema. `make kb-snapshot ACTOR=<id>` builds an immutable collection and manifest. A release is activated only when the candidate report, PASS gate report, independent-test metadata, exact snapshot collection and source hash agree: `make release-activate SNAPSHOT=<id> CANDIDATE_REPORT=<path> GATE_REPORT=<path> CANDIDATE=<name> APPROVED_BY=<id>`. Rollback is `make release-rollback RELEASE=<id> ACTOR=<id> REASON=<text>`. Activation/rollback returns `restart_required=true`; restart the app so the saved runtime config and active collection are loaded together. Credentials remain in `.env` and are not copied into release records.

The RoBERTa diagnosis workflow is invoked with `make diagnose-cases`. It fingerprints the classifier artifact and analysis configuration, classifies topic labels for bucketing, and stores evidence-referenced LLM fault proposals without confirming them. The case-pair review page is `/review/pairs`; it records human final labels and append-only review events. The metrics endpoint reports only the human-reviewed recorded sample and must not be presented as a population estimate. Stratified audits of unproposed same-bucket, cross-bucket-neighbor and random pairs remain necessary for estimating missed merges.

These additions have passed static Python compilation and whitespace checks only. Database migrations, ONNX classification, model diagnosis, Milvus alias switching and rollback still require live environment acceptance; they have not been represented as runtime-verified.

## Current local runtime status (2026-10-26)

Docker services were started and ch12-ch16 migrations applied. The API docs endpoint responds with HTTP 200, and MySQL/Milvus report healthy. The intake gate now retains obvious one-character/numeric noise cases as `ignored`, and the live rerun did not recreate review candidates. Retrieval evaluation is currently incomplete because the configured SiliconFlow embedding/rerank account has insufficient balance; no quality pass is claimed. The RoBERTa ONNX artifacts are absent, so diagnosis runtime acceptance remains pending.
