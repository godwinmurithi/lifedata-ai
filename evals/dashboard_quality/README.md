# VIMA dashboard quality benchmark

This benchmark drives the actual `/lifedata-ai/api/chat/` conversation endpoint, retaining SSE tool coverage, errors, publication events and stop reasons. It does not treat a fluent answer, a tool invocation or an unreviewed screenshot as proof of quality.

Run `python evals/dashboard_quality/run.py` to list cases without model calls. For one live, billed conversation, supply a current permitted user's session cookie in `LIFEDATA_EVAL_SESSION_COOKIE` and run `python evals/dashboard_quality/run.py --live --dataset-id 8 --case general-monitoring`. Configure `LIFEDATA_EVAL_BASE_URL` and `LIFEDATA_EVAL_ORIGIN` for your deployment. Never put a cookie in a command argument, artifact or commit. The runner does not approve pending actions: use VIMA's normal authorized execution flow if a conversation pauses for approval.

Reports default to `/tmp/vima-dashboard-eval`. Conversations may contain sensitive dataset metadata: retain them according to the deployment's data policy. No session cookie is written to the report.

Score existing traces without a model call using `--trace /path/report.json`. Use `--evidence /path/evidence.json` for independent assessment. Each of `data_correctness`, `visual_quality`, `reference_fidelity` and `interactions` takes an object with `status` (`pass` or `fail`), `artifact` (path to reviewable evidence), `reviewer` and explanatory `notes`. Missing evidence remains `unverified`; the benchmark exits nonzero until every dimension passes.

Data evidence should compare full server-side totals, denominators and missingness with rendered values. Visual evidence should include matching desktop/mobile screenshots. Reference evidence should compare the supplied template at matching viewport (or document that the case has no fixed reference and assess its requested design brief). Interaction evidence should cover every chart tab, a filter change/reset and navigation. Review the actual artifacts before supplying pass results. These dimensions remain separate so improvements in appearance cannot mask numerical errors.

No model quality result is claimed merely because the runner's unit tests pass.
