# The /migrate prompt

Use this in step 13 of [PARTICIPANT_CHECKLIST.md](PARTICIPANT_CHECKLIST.md),
which contains the same text. Inside Droid, type `/migrate`, press Enter, then
paste everything in the box below. (You can also type `/migrate ` followed by
the prompt on one line.)

Run it only after the readiness steps and after you have approved the design
doc (step 11). The prompt treats `docs/migration-design.md` as the spec:
`/migrate` builds what the doc decides and stops to ask if it finds the design
is wrong.

```text
Migrate the PAYOPS_CROSS_BORDER_RECONCILIATION batch in this repository from Control-M to Apache Airflow 3, side by side with the existing Control-M setup, and prove the Airflow version behaves the same.

The design is decided
- docs/migration-design.md is the spec. Build what its mapping table and DECIDED decisions say. Do not re-ask questions it already answers.
- If you find the design is wrong, incomplete, or contradicts the Control-M definitions, stop and ask me. Never change a decision on your own. After I answer, update the doc so it stays an accurate record of what was built.

Sources of truth
- Control-M definitions: repos/payments-orchestrator/controlm/ (Automation API JSON, legacy XML export, task-commands.json, calendars.json).
- Business logic lives in the eight repositories under repos/. Every Control-M job only calls the repos/payments-orchestrator/scripts/run-task.sh wrapper. Migrate the orchestration, not the services.
- Legacy behaviour: scripts/controlm-run.sh <scenario> runs the batch through the Control-M compatibility harness. runtimes/control-m/harness/verify_run.py grades a run directory against fixtures/scenarios/<scenario>/expected/manifest.json. fixtures/CONTRACT.md describes the run directory, exit codes, and business rules.
- AGENTS.md describes how to work in this repository.

Deliverables
- The Airflow DAG the design doc describes, calling the existing wrapper.
- The local Airflow environment the design doc describes, pinned to an exact Airflow 3 version, bound to 127.0.0.1 only, with documented start, stop, and trigger commands.
- One command that runs every check below.

What done means (prove each with evidence)
1. The DAG imports with no errors, has 12 tasks and no cycles, and its edges match the Control-M conditions exactly.
2. All five scenarios (happy-path, duplicate-retry, business-cutoff, partial-ledger-write, reconciliation-breaks) run in Airflow and pass the existing verify_run.py oracle. Read each scenario.json: some inject a fault and some require the batch to be re-run.
3. For every scenario, Airflow output matches legacy harness output after normalization (verify_run.py --normalize).
4. In partial-ledger-write, post_pending_ledger fails once, retries, and still posts each payment exactly once. No other task retries.
5. The DAG's schedule produces the same 2026 run dates as the Control-M definition and business calendar, in the timezone the design doc decided.
6. Every repository's tests and scripts/test-controlm-compatibility.sh still pass.

Rules
- Never change fixtures, expected manifests, or service logic to make a check pass. Do not weaken verify_run.py. If it must learn to accept an Airflow runtime, that is the only change allowed there, and the design doc must say so. If you believe a fixture or rule is wrong, stop and ask me.
- Keep the Control-M definitions and legacy harness working. This is not a cutover.
- Tasks make no network calls and send no real email.
- No secrets in the repository. Do not push.

When you finish, give me: the commands you ran and their results, the one command I run to repeat every check, every change you made to the design doc and why, and anything you could not prove.
```
