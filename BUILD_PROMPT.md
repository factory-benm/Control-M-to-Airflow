# The /migrate prompt

Use this in step 12 of [PARTICIPANT_CHECKLIST.md](PARTICIPANT_CHECKLIST.md).
Inside Droid, type `/migrate`, press Enter, then paste everything in the box
below. (You can also type `/migrate ` followed by the prompt on one line.)

Do this only after the readiness steps and the design doc. `/migrate` reads
both.

```text
Migrate the PAYOPS_CROSS_BORDER_RECONCILIATION batch in this repository from Control-M to Apache Airflow 3, side by side with the existing Control-M setup, and prove the Airflow version behaves the same.

Sources of truth
- Control-M definitions: repos/payments-orchestrator/controlm/ (Automation API JSON, legacy XML export, task-commands.json, calendars.json).
- Business logic lives in the eight repositories under repos/. Every Control-M job only calls the repos/payments-orchestrator/scripts/run-task.sh wrapper. Migrate the orchestration, not the services.
- Legacy behaviour: scripts/controlm-run.sh <scenario> runs the batch through the Control-M compatibility harness. runtimes/control-m/harness/verify_run.py grades a run directory against fixtures/scenarios/<scenario>/expected/manifest.json. fixtures/CONTRACT.md describes the run directory, exit codes, and business rules.
- AGENTS.md and docs/migration-design.md. Follow the design doc. If you find it is wrong, fix the doc and tell me why.

What to build
- An Airflow DAG that runs the 12 jobs through the existing wrapper, with dependencies, retries, schedule, business calendar, batch window, timezone, variables, and failure handling taken from the Control-M definitions. Anything with no direct Airflow equivalent must be called out, not silently dropped.
- A local Airflow environment pinned to an exact Airflow 3 version (Docker Compose is fine), bound to 127.0.0.1 only, with documented start, stop, and trigger commands.
- One command that runs every check below.

What done means (prove each with evidence)
1. The DAG imports with no errors, has 12 tasks and no cycles, and its edges match the Control-M conditions exactly.
2. All five scenarios (happy-path, duplicate-retry, business-cutoff, partial-ledger-write, reconciliation-breaks) run in Airflow and pass the existing verify_run.py oracle. Read each scenario.json: some inject a fault and some require the batch to be re-run.
3. For every scenario, Airflow output matches legacy harness output after normalization (verify_run.py --normalize).
4. In partial-ledger-write, post_pending_ledger fails once, retries, and still posts each payment exactly once. No other task retries.
5. The DAG's schedule produces the same 2026 run dates as the Control-M definition and business calendar, in Asia/Singapore time.
6. Every repository's tests and scripts/test-controlm-compatibility.sh still pass.

Rules
- Never change fixtures, expected manifests, or service logic to make a check pass. Do not weaken verify_run.py. If it must learn to accept an Airflow runtime, that is the only change allowed there, and you must explain it. If you believe a fixture or rule is wrong, stop and ask me.
- Keep the Control-M definitions and legacy harness working. This is not a cutover.
- Tasks make no network calls and send no real email. Turn the Control-M failure mail into a logged callback.
- No secrets in the repository. Do not push.

When you finish, give me: the Control-M to Airflow mapping table, the commands you ran and their results, the one command I run to repeat every check, and anything you could not prove.
```
