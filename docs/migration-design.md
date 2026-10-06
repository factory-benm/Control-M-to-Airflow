# Migration design: PAYOPS_CROSS_BORDER_RECONCILIATION, Control-M to Apache Airflow 3

Status: APPROVED. Every decision in section 8 is DECIDED (2026-10-06).
Written 2026-10-06. Changes made after the first draft are listed in section 9.

This document is the spec that `/migrate` builds from (see `BUILD_PROMPT.md`).
It covers one Control-M folder of 12 jobs. It migrates the orchestration only.
The eight repositories under `repos/` and the `run-task.sh` wrapper are not
changed.

Sources read for this design:

- `repos/payments-orchestrator/controlm/payments_reconciliation.json` (called "JSON" below)
- `repos/payments-orchestrator/controlm/payments_reconciliation.xml` ("XML")
- `repos/payments-orchestrator/controlm/task-commands.json` ("task-commands")
- `repos/payments-orchestrator/controlm/calendars.json` ("calendars.json")
- `repos/payments-orchestrator/src/orchestrator/graph.py` and `scripts/run-task.sh`
- `fixtures/CONTRACT.md`, `fixtures/calendars/sg-business-calendar-2026.json`, all five `fixtures/scenarios/*/scenario.json` and `expected/manifest.json`
- `runtimes/control-m/harness/harness.py`, `runtimes/control-m/harness/verify_run.py`, `scripts/test-controlm-compatibility.sh`

All line references are to the files as they are at commit `7601515`.

---

## 1. What Control-M does for this batch today

**One folder, twelve command jobs, one straight line.** Folder
`PAYOPS_CROSS_BORDER_RECONCILIATION` on Control-M server `PAYOPS-CTM` holds 12
`Job:Command` jobs. Each job runs the same wrapper,
`%%ORCHESTRATOR_HOME/scripts/run-task.sh --task <job> ...`, which looks up the
owning repository in task-commands and execs that repository's own
`run-task.sh` (`run-task.sh:78-122`). The wrapper passes exit codes through
unchanged (`run-task.sh:25-30`).

**Dependencies are event conditions.** Each job adds
`PAYOPS-<JOB_UPPER>-OK` on success. The next job waits for that condition and
deletes it when it starts. Conditions are scoped to the order date (`ODATE="ODAT"`
in the XML). The result is a linear chain of 11 edges:

```
watch_inbound_files -> verify_file_integrity -> extract_payment_batch
  -> validate_payment_schema -> deduplicate_payments -> enrich_fx_rates
  -> apply_business_day_cutoff -> post_pending_ledger
  -> reconcile_nostro_ledger -> classify_breaks
  -> produce_settlement_report -> archive_and_notify
```

The JSON also declares a `Flow` sequence (`JSON:260-276`) that repeats the
same order. `graph.py:201-216` checks it against the conditions.

**Scheduling.** Every job inherits a `When` block from the JSON defaults:
Monday to Friday, restricted to calendar `PAYOPS_SG_BUSINESS_2026`, with a
submission window from 20:00 to 06:00 that crosses midnight (`JSON:17-25`). No
timezone is stated in either definition; only calendars.json says
`Asia/Singapore` (`calendars.json:7`). The folder's order method is `Manual`
(`JSON:41`, `XML:16`), so Control-M does not order it automatically at New Day.
It runs only when an operator orders it.

**Retries.** Only `post_pending_ledger` reruns: up to 2 times, 1 minute after
the previous run ends (`JSON:166-173`, `XML:194-195`, task-commands `84-85`).
All other jobs have 0 reruns. Posting is idempotent by `payment_id`
(`CONTRACT.md:355-368`), which is why the rerun is safe.

**Failure handling.** When a job ends NOTOK, Control-M sends a mail to
payments operations (`JSON:26-34`; per-job copies in the XML). The downstream
condition is never added, so the rest of the chain does not run.

**Variables.** Seven folder-default variables feed every command line
(`JSON:7-15`): `RUN_ID`, `SCENARIO`, `RUN_DIR`, `KIT_ROOT`,
`ORCHESTRATOR_HOME`, `FIXED_CLOCK`, `ATTEMPT`. Their values are hard-coded to
the happy-path scenario.

**State between jobs** lives only in the run directory on disk
(`workspace/runtime/runs/<run-id>/`, `CONTRACT.md:161-176`). Jobs pass nothing
to each other through the scheduler.

**What the compatibility harness adds.** We have no real Control-M here. The
harness (`harness.py`) runs the graph the JSON declares and is what the
fixtures were proven against. It does several things Control-M itself does not
do, and the Airflow version must reproduce them to be graded the same way:

- clears and creates the run directory and its five subdirectories (`harness.py:192-201`)
- writes `runtime.json` (`harness.py:203-233`)
- passes the real attempt number as `--attempt` and sets `PAYOPS_*` environment variables (`harness.py:255-281`)
- captures stdout and stderr per pass and attempt to `logs/<task>.pass<P>.attempt<A>.stdout|stderr` (`harness.py:253-254`)
- retries only exit code 3, only when the scenario declares that fault, up to the task's `retryLimit`, with no delay; any other failure stops the run (`harness.py:345-374`)
- runs the whole graph a second time when a scenario declares a rerun and no fault (`harness.py:529-533`), which is how `duplicate-retry` works
- writes `run-manifest.json` with the exit sequence, counts, ledger rows per pass, input and output hashes, and observed invariants (`harness.py:540-566`)

It does not implement time windows, calendars, or mail (`harness.py:23-25`).

`verify_run.py` then grades a run directory against
`fixtures/scenarios/<scenario>/expected/manifest.json`. It only accepts the
runtime modes `compatibility-harness` and `control-m-workbench`
(`verify_run.py:130-134`).

---

## 2. Mapping

### 2.1 Jobs

Every job maps to one Airflow task with the same name (task_id = Control-M job
name). Every task calls the existing wrapper with the same arguments as the
Control-M command line. Every `eventsToWaitFor` / `INCOND` becomes exactly one
upstream dependency with the default `all_success` trigger rule. No other
dependencies exist.

| # | Control-M job | JSON | XML | task-commands | Repository | Waits for condition | Airflow task | Upstream task | Retries |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 1 | `watch_inbound_files` | 44-52 | 20-44 | 8-17 | payment-ingestion (11) | none | `watch_inbound_files` | none | 0 |
| 2 | `verify_file_integrity` | 54-70 (wait 58-61) | 46-67 (INCOND 61) | 18-27 | payment-ingestion (21) | `PAYOPS-WATCH_INBOUND_FILES-OK` | `verify_file_integrity` | `watch_inbound_files` | 0 |
| 3 | `extract_payment_batch` | 72-88 (wait 76-79) | 69-90 (INCOND 84) | 28-37 | payment-ingestion (31) | `PAYOPS-VERIFY_FILE_INTEGRITY-OK` | `extract_payment_batch` | `verify_file_integrity` | 0 |
| 4 | `validate_payment_schema` | 90-106 (wait 94-97) | 92-113 (INCOND 107) | 38-47 | payment-validation (41) | `PAYOPS-EXTRACT_PAYMENT_BATCH-OK` | `validate_payment_schema` | `extract_payment_batch` | 0 |
| 5 | `deduplicate_payments` | 108-124 (wait 112-115) | 115-136 (INCOND 130) | 48-57 | payment-validation (51) | `PAYOPS-VALIDATE_PAYMENT_SCHEMA-OK` | `deduplicate_payments` | `validate_payment_schema` | 0 |
| 6 | `enrich_fx_rates` | 126-142 (wait 130-133) | 138-159 (INCOND 153) | 58-67 | fx-enrichment (61) | `PAYOPS-DEDUPLICATE_PAYMENTS-OK` | `enrich_fx_rates` | `deduplicate_payments` | 0 |
| 7 | `apply_business_day_cutoff` | 144-160 (wait 148-151) | 161-182 (INCOND 176) | 68-77 | ledger-reconciliation (71) | `PAYOPS-ENRICH_FX_RATES-OK` | `apply_business_day_cutoff` | `enrich_fx_rates` | 0 |
| 8 | `post_pending_ledger` | 162-186 (wait 174-177, rerun 166-173) | 184-206 (INCOND 200, MAXRERUN 194) | 78-87 (retry 84-85) | ledger-reconciliation (81) | `PAYOPS-APPLY_BUSINESS_DAY_CUTOFF-OK` | `post_pending_ledger` | `apply_business_day_cutoff` | **2, 60 s apart** |
| 9 | `reconcile_nostro_ledger` | 188-204 (wait 192-195) | 208-229 (INCOND 223) | 88-97 | ledger-reconciliation (91) | `PAYOPS-POST_PENDING_LEDGER-OK` | `reconcile_nostro_ledger` | `post_pending_ledger` | 0 |
| 10 | `classify_breaks` | 206-222 (wait 210-213) | 231-252 (INCOND 246) | 98-107 | exception-management (101) | `PAYOPS-RECONCILE_NOSTRO_LEDGER-OK` | `classify_breaks` | `reconcile_nostro_ledger` | 0 |
| 11 | `produce_settlement_report` | 224-240 (wait 228-231) | 254-275 (INCOND 269) | 108-117 | settlement-reporting (111) | `PAYOPS-CLASSIFY_BREAKS-OK` | `produce_settlement_report` | `classify_breaks` | 0 |
| 12 | `archive_and_notify` | 242-258 (wait 246-249) | 277-298 (INCOND 292) | 118-127 | payment-notifications (121) | `PAYOPS-PRODUCE_SETTLEMENT_REPORT-OK` | `archive_and_notify` | `produce_settlement_report` | 0 |

Each job's `Description` (JSON) / `DESCRIPTION` (XML) becomes the task's
`doc_md`.

### 2.2 Folder, default, and job settings

| Control-M setting | Control-M value (file:line) | Airflow 3 equivalent |
| --- | --- | --- |
| Folder name | `PAYOPS_CROSS_BORDER_RECONCILIATION` (JSON:38, XML:15) | `dag_id` `PAYOPS_CROSS_BORDER_RECONCILIATION`, unchanged for traceability. |
| Folder type | `Folder` (JSON:39) | One DAG. |
| Folder comment | "Cross-border payments reconciliation batch." (JSON:42) | DAG `description`. |
| Control-M server | `PAYOPS-CTM` (JSON:40), `DATACENTER` (XML:12) | No equivalent. The Airflow deployment is the server. |
| XML folder metadata | `VERSION="900"`, `PLATFORM="UNIX"`, `TYPE`, `USED_BY_CODE` (XML:13-18) | Not migrated. Export metadata only. |
| Order method | `Manual` (JSON:41, XML:16) | DAG created paused (`is_paused_upon_creation=True`). See Decision 4. |
| Application | `PAYOPS` (JSON:3, XML:23 etc.) | DAG `tags`. |
| Sub-application | `CROSS_BORDER_RECONCILIATION` (JSON:4, XML:24 etc.) | DAG `tags`. |
| Run as | `payops` (JSON:5, `RUN_AS` XML:26 etc.) | No equivalent locally. Tasks run as the user running Airflow. `run_as_user` is not used (it needs sudo). Gap documented in section 3. |
| Host | `payops-batch-host` (JSON:6, `NODEID` XML:27 etc.) | No equivalent locally. Single machine, LocalExecutor. |
| Job type | `Job:Command` (JSON:45 etc.), `TASKTYPE="Command"` (XML:28 etc.) | A task that runs the wrapper as a subprocess. See Decision 8. |
| Command | `%%ORCHESTRATOR_HOME/scripts/run-task.sh --task <job> --run-dir %%RUN_DIR --scenario %%SCENARIO --run-id %%RUN_ID --kit-root %%KIT_ROOT --now %%FIXED_CLOCK --attempt %%ATTEMPT` (JSON:47 etc., `CMDLINE` XML:29 etc.) | Same wrapper, same arguments, same order, values from DAG params (below). Also sets `PAYOPS_KIT_ROOT`, `PAYOPS_FIXED_CLOCK`, `PAYOPS_ATTEMPT`, `PAYOPS_RUN_ID`, `PAYOPS_SCENARIO` as the harness does (`harness.py:272-281`). |
| `MEMNAME` | job name (XML:25 etc.) | Not needed. |
| Variable `RUN_ID` | `baseline-happy-path` (JSON:8, XML:37) | DAG param `run_id`. See Decision 15. |
| Variable `SCENARIO` | `happy-path` (JSON:9, XML:38) | DAG param `scenario`, default `happy-path`. |
| Variable `RUN_DIR` | `/workspace/runtime/runs/baseline-happy-path` (JSON:10) | Derived: `<kit_root>/workspace/runtime/runs/<run_id>`. Not a separate param, so it cannot disagree with `run_id`. |
| Variable `KIT_ROOT` | `/workspace` (JSON:11) | DAG param `kit_root`, default the repository root resolved from the DAG file location. |
| Variable `ORCHESTRATOR_HOME` | `/workspace/repos/payments-orchestrator` (JSON:12) | Derived: `<kit_root>/repos/payments-orchestrator`. |
| Variable `FIXED_CLOCK` | `2026-03-16T09:30:00Z` (JSON:13) | DAG param `fixed_clock`; when empty, read `clock.fixedUtc` from the scenario's `scenario.json` (`CONTRACT.md:423-431`). Never taken from the Airflow logical date. |
| Variable `ATTEMPT` | `1`, static (JSON:14, XML:39) | Airflow task instance `try_number` (1 on the first try, 2 on the first retry). See Decision 7. |
| Week days | `MON`-`FRI` (JSON:18), `WEEKDAYS="12345"` (XML:31 etc.) | Built into the timetable. See Decision 3. |
| Month days | `NONE` (JSON:19) | Nothing to map. |
| Calendar | `RuleBasedCalendars.Included: PAYOPS_SG_BUSINESS_2026` (JSON:20-22), `DAYSCAL="PAYOPS_SG_BUSINESS_2026"` (XML:32 etc.); dates in calendars.json:13-24 | Timetable that fires only on 2026 weekdays not listed in calendars.json `excludedDates`. See Decision 3. |
| Timezone | Not stated in JSON or XML. `Asia/Singapore` in calendars.json:7 and fixture calendar:47 | DAG timezone. See Decision 2. |
| From time | `2000` (JSON:23; XML:33 on `watch_inbound_files` only) | Timetable fires at 20:00 on each business day. |
| To time | `0600` next day (JSON:24; XML:34 on `watch_inbound_files` only) | No direct equivalent. See section 3 and Decision 5. |
| Rerun limit | `post_pending_ledger`: `Times: 2` (JSON:166-168), `MAXRERUN="2"` (XML:194), `retryLimit: 2` (task-commands:84); all others 0 (`MAXRERUN="0"`, `retryLimit: 0`) | `retries=2` on `post_pending_ledger`; `retries=0` on every other task, set explicitly. |
| Rerun interval | `Every: 1, Units: Minutes, From: End` (JSON:169-173), `RERUNINTERVAL="00001M"` (XML:195), `retryDelaySeconds: 60` (task-commands:85) | `retry_delay` of 60 seconds, `retry_exponential_backoff=False`. Airflow measures the delay from the failed try's end, which matches `From: End`. |
| Which failures rerun | Control-M reruns on any NOTOK. CONTRACT allows retrying only exit 3, only when the scenario declares it (`CONTRACT.md:156-157`) | See Decision 6. |
| Wait for condition | `eventsToWaitFor` (JSON), `INCOND ... AND_OR="A"` (XML) | Upstream dependency, `all_success`. |
| Add condition | `eventsToAdd` (JSON), `OUTCOND SIGN="+"` (XML) | Implicit: task success lets the downstream task run. |
| Delete condition | `eventsToDelete` (JSON), `OUTCOND SIGN="-"` (XML) | Implicit: Airflow dependency state belongs to one DAG run and cannot leak into another. |
| Condition date scope | `ODATE="ODAT"` (XML:40 etc.) | Implicit: per DAG run. |
| Flow | `reconciliation_flow` sequence (JSON:260-276) | The same 11 edges. The structural test checks the DAG against it. |
| Confirm | `CONFIRM="0"` (XML:35 etc.) | Nothing to map (no manual confirmation). |
| Critical | `CRITICAL="1"` on 10 jobs, `"0"` on `classify_breaks` (XML:245) and `archive_and_notify` (XML:291); `critical` in task-commands (106, 126); absent in JSON | No behaviour. Recorded in each task's `doc_md`. See Decision 14. |
| On NOTOK, send mail | JSON default: one action, to `payments-operations@payops.invalid`, message `%%JOBNAME failed for run %%RUN_ID scenario %%SCENARIO` (JSON:26-34). XML: one `DOMAIL` per job, three recipients (XML:41-43 etc.) | `on_failure_callback` that logs the would-be mail. Nothing is sent. See Decision 10. |
| Concurrency | One order per order date (implicit) | `max_active_runs=1`, `catchup=False`. |

---

## 3. Settings with no direct Airflow equivalent

| Control-M behaviour | Why there is no direct equivalent | How we handle it |
| --- | --- | --- |
| Business calendar `PAYOPS_SG_BUSINESS_2026` | Airflow has no holiday calendars. Cron cannot exclude dates. | Build the run dates from calendars.json at DAG parse time (Decision 3). A test checks calendars.json and the fixture calendar list the same holidays (they do today: 10 identical dates). |
| Submission window 20:00 to 06:00 | Airflow starts a run at its scheduled time. It has no "do not submit after" setting. Task SLAs were removed in Airflow 3. | Schedule at 20:00. Do not enforce 06:00 in tasks, because manual and test runs must run at any time (the harness ignores windows too). Optionally log a warning if a scheduled run has not finished by 06:00 (Decision 5). |
| `OrderMethod: Manual` | Airflow either schedules a DAG or it does not. | DAG starts paused, with its schedule defined. Nothing runs until an operator unpauses or triggers it (Decision 4). |
| Static `%%ATTEMPT` | Control-M would need `%%RUNCOUNT`; the definitions do not use it. | Pass Airflow `try_number` (Decision 7). |
| Retry only on exit 3 | Neither Control-M nor Airflow retries by exit code natively. | Task code fails without retry (Airflow `AirflowFailException`) for any exit code other than a declared exit 3 (Decision 6). |
| Mail on NOTOK | Rule: tasks make no network calls and send no email. | Log-only failure callback (Decision 10). |
| `RunAs payops`, host `payops-batch-host` | Local Airflow runs everything as one user on one machine. | Documented gap. Production would map these to a deployment user and a worker queue; out of scope here. |
| `CRITICAL` flag | Airflow `priority_weight` only orders queued tasks; in a straight chain only one task is ever ready. | Documentation only (Decision 14). |
| Whole-folder rerun (operator re-orders or reruns the folder) | Clearing an Airflow DAG run raises `try_number`, so tasks would get `--attempt 2`. | A second DAG run on the same run directory with `pass=2` (Decision 12). |
| Harness duties: run-dir setup, `runtime.json`, per-attempt logs, `run-manifest.json`, invariants | Not scheduler features; the harness invented them for grading. | Task code captures per-attempt logs and records. A runner script prepares the run directory, triggers the DAG, and builds `run-manifest.json` (Decision 11). |

---

## 4. Where the definitions disagree with each other or with the fixtures

`graph.py:99-125` (used by `./scripts/controlm-validate.sh` and the harness)
only compares task names, edges, and retry counts. All three files agree on
those. Everything below passes validation today without being noticed.

`graph.py:5-6` names the JSON as the authority. Decision 1 asks you to confirm
that rule; the recommendation column assumes it.

| # | Disagreement | Where | Effect on the migration | Recommendation |
| --- | --- | --- | --- | --- |
| D1 | **Time window scope.** JSON puts `FromTime 2000 / ToTime 0600` in the job defaults, so all 12 jobs have it. XML sets `TIMEFROM/TIMETO` on `watch_inbound_files` only. | JSON:23-24; XML:33-34 (absent at XML:46-298) | Decides whether the window applies to the first job or every job. | Follow JSON. Since the window is only used as the 20:00 start (Decision 5), the practical difference is nil. |
| D2 | **Variables.** JSON defines 7 folder-wide variables. XML defines only `RUN_ID`, `SCENARIO`, `ATTEMPT`, and only on `watch_inbound_files`; job-level variables are local to that job in Control-M. `RUN_DIR`, `KIT_ROOT`, `ORCHESTRATOR_HOME`, `FIXED_CLOCK` are defined nowhere in the XML, yet every `CMDLINE` uses them. | JSON:7-15; XML:37-39 | The XML as written would not resolve its own command lines. | Follow JSON. Treat the XML as a structural export only. |
| D3 | **Failure mail.** JSON sends every failure to `payments-operations@payops.invalid` with one message. XML routes `classify_breaks` to `reconciliation-operations@payops.invalid` and `produce_settlement_report` to `settlement-reporting@payops.invalid`, marks `post_pending_ledger` urgent (`URGENCY="U"`) with its own "rerun is safe" message, and uses per-job subjects. | JSON:26-34; XML:204, 250, 273 | Decides what the failure callback logs. | Decision 10. |
| D4 | **Critical flag.** XML and task-commands mark 10 jobs critical and 2 not. JSON sets `Critical` on no job, which in Automation API means no job is critical. | XML:36, 245, 291 etc.; task-commands:16, 106, 126 etc.; JSON (absent) | Documentation only. | Decision 14. |
| D5 | **Calendar type.** JSON references a rule-based calendar (`RuleBasedCalendars`). XML references a regular days calendar (`DAYSCAL`) combined with `WEEKDAYS`, and sets no `DAYS_AND_OR`. calendars.json is a plain list of excluded dates, which is neither. | JSON:20-22; XML:31-32 etc.; calendars.json:13-24 | In real Control-M these can produce different run days. The intended meaning (weekdays minus listed holidays) is clear from calendars.json and the fixture calendar. | Implement "Monday to Friday, excluding calendars.json `excludedDates`" (254 dates in 2026). Ask a Control-M administrator to confirm. |
| D6 | **Attempt number vs. the partial-ledger-write fixture.** Both definitions pass a static `ATTEMPT=1`. A real Control-M rerun would send `--attempt 1` again, the injected fault (declared for attempt 1) would fire on every rerun, and `post_pending_ledger` would fail after 3 runs. The fixture expects attempt 1 to exit 3 and attempt 2 to exit 0. The harness passes the real attempt number, so it does not follow the definition here. | JSON:14; XML:39; `partial-ledger-write/scenario.json:62-71, 101-107`; `harness.py:269-270` | Copying the definition literally would fail scenario 4. | Pass `try_number` (Decision 7). Record that this deliberately differs from the literal definition. |
| D7 | **Retry trigger.** Control-M reruns on any NOTOK. CONTRACT and the harness retry only exit 3, only when the scenario declares it, and stop on anything else. | JSON:166-173; XML:194; `CONTRACT.md:156-157`; `harness.py:356-368` | With literal Control-M semantics, exit 1, 4, or 5 from `post_pending_ledger` would retry twice. | Decision 6. |
| D8 | **Retry delay in the harness.** The definitions say 1 minute. The harness retries immediately. | JSON:169-173; task-commands:85; `harness.py:369-374` | Airflow with a 60 s delay is slower than the harness but the exit sequence is the same. | Keep 60 s in Airflow (Decision 6). |
| D9 | **Scenario values hard-coded.** `RUN_ID`, `SCENARIO`, and `FIXED_CLOCK` are fixed to happy-path. The other four scenarios use other clocks (`2026-03-17T09:30:00Z`, `2026-03-19T16:00:00Z`, `2026-03-16T10:00:00Z`, `2026-03-16T11:00:00Z`). The harness reads them from `scenario.json` instead. | JSON:8-13; XML:37-38; each `scenario.json:4`; `harness.py:181` | The DAG must take these as parameters. | Params with JSON values as defaults; clock from `scenario.json` when not given. |
| D10 | **Absolute container paths.** `RUN_DIR=/workspace/runtime/runs/...` and `KIT_ROOT=/workspace` do not exist on a workstation. CONTRACT puts runs at `workspace/runtime/runs/<run-id>/` under the repository. | JSON:10-12; `CONTRACT.md:163-166` | Literal values would write outside the repository. | Derive paths from the repository root (Decision 15). |
| D11 | **Order method vs. schedule.** The folder is `Manual`, so its `When` block never fires on its own, yet the batch is described as overnight and scheduled. | JSON:41, 17-25; XML:16 | Decides whether the DAG runs on its own. | Decision 4. |
| D12 | **Calendar names.** The scheduler calendar is `PAYOPS_SG_BUSINESS_2026`. The fixture calendar, and every expected manifest, call it `PAYOPS-SG-2026`. The holiday dates are identical. | calendars.json:6; fixture calendar:2; e.g. `partial-ledger-write/expected/manifest.json:12` | None for behaviour. The cutoff task writes the fixture id. | Keep both names. The DAG refers to `PAYOPS_SG_BUSINESS_2026`; outputs keep `PAYOPS-SG-2026`. |
| D13 | **Fixed clocks fall outside the window.** Four scenario clocks are between 17:30 and 19:00 Singapore time, before the 20:00 window opens. `business-cutoff` is at 00:00 on 2026-03-20, a declared holiday, which is only valid as part of the 2026-03-19 order date. | each `scenario.json:4`; JSON:23-24 | If the DAG used the fixed clock as its logical date, or enforced the window, these scenarios could not run. | The fixed clock is a business clock passed to tasks. It is never the schedule (D9, Decision 5). |
| D14 | **Whole-batch rerun vs. expected exit sequence.** `duplicate-retry` expects 12 exit entries, but the harness runs the graph twice and records 24. `verify_run.py` accepts that only because the second half repeats the first exactly. | `duplicate-retry/expected/manifest.json`; `harness.py:532-533`; `verify_run.py:190-196` | The Airflow version must record both passes the same way, each starting at attempt 1. | Decision 12. |
| D15 | **Grader runtime allow-list.** `verify_run.py` rejects any runtime mode other than `compatibility-harness` and `control-m-workbench`. | `verify_run.py:130-134` | Airflow runs fail "runtime mode recorded" no matter how correct they are. | Decision 11. |

---

## 5. Target layout and local Airflow setup

### 5.1 Layout

New files only. Nothing under `repos/`, `fixtures/`, or `runtimes/control-m/`
changes, except the single allow-list entry in `verify_run.py` if Decision 11
is accepted.

```
runtimes/airflow/
  README.md                         start, stop, trigger, and test commands
  airflow-version.env               exact Airflow version, Python version, constraints URL
  dags/
    payops_cross_border_reconciliation.py   the DAG (one file)
  tests/                            DAG structure and schedule tests (unittest)
scripts/
  airflow-up.sh                     install the pin (first time), start Airflow on 127.0.0.1
  airflow-down.sh                   stop Airflow
  airflow-run.sh <scenario>         prepare run dir, trigger DAG run(s), wait, build run-manifest.json
  test-airflow-equivalence.sh       every Airflow check in section 6, one command
docs/migration-design.md            this file
workspace/airflow/                  AIRFLOW_HOME: venv, metadata DB, logs, generated admin password (git-ignored)
```

The DAG does not go in `repos/payments-orchestrator/` because that repository
carries a `STDLIB_ONLY` marker and the DAG imports Airflow (Decision 13). It
sits beside `runtimes/control-m/`, matching the existing runtime pattern.

The DAG reads, and never writes: task-commands (task list, dependencies,
retry limits), the JSON (through `graph.py`, for the canonical edges),
calendars.json (run dates), and `fixtures/scenarios/<scenario>/scenario.json`
(default fixed clock and declared fault). It imports `orchestrator.graph` from
`repos/payments-orchestrator/src`, which is standard library only.

### 5.2 What a task does

For each try, the task:

1. Ensures the run directory and its `input/`, `stages/`, `logs/`, `ledger/`, `output/` subdirectories exist. It creates missing ones and never deletes anything.
2. Runs `repos/payments-orchestrator/scripts/run-task.sh` with the section 2.2 arguments, `--attempt` set to `try_number`, and the `PAYOPS_*` environment variables.
3. Writes stdout and stderr to `logs/<task>.pass<P>.attempt<A>.stdout` and `.stderr`, the harness naming, so `verify_run.py:317-345` can check them.
4. Appends one record (task, repository, pass, attempt, exit code, counts, metrics, artifacts, log paths, timestamps) to `logs/airflow-attempts.jsonl`. This file is outside `stages/`, so it is not part of the normalized comparison.
5. Succeeds on exit 0. On exit 3, when the scenario declares that fault for this task and attempt, fails so Airflow retries. On any other non-zero exit, fails without retry.

No data moves through XCom. The run directory is the only shared state, as in
Control-M.

### 5.3 Local Airflow

- Exact Airflow 3 version pinned in `runtimes/airflow/airflow-version.env`, installed with the official constraints file for that version into a virtual environment under `workspace/airflow/` (Decision 13). The services stay standard library only; Airflow exists only in this runtime venv.
- `AIRFLOW_HOME=workspace/airflow`, `dags_folder=runtimes/airflow/dags`, example DAGs off.
- The same four components `airflow standalone` runs, started one by one by `scripts/airflow-up.sh` (Decision 17): `airflow scheduler --skip-serve-logs`, `airflow dag-processor`, `airflow api-server --host 127.0.0.1 --port <port>`, `airflow triggerer --skip-serve-logs`. SQLite metadata DB, LocalExecutor, SimpleAuthManager (the settings `standalone` forces). Confirmed on 3.3.2: LocalExecutor is the default executor and runs on SQLite. `airflow standalone` is not used because in 3.3.2 it starts the scheduler and triggerer log servers bound to all interfaces (`airflow/utils/serve_logs/core.py` binds `host=""` on ports 8793 and 8794, with no setting to change it). With LocalExecutor the API server reads task logs from local files, so the log servers are not needed.
- API server bound to `127.0.0.1` only, port 8080 unless taken. The task runtime's calls to the API server (`execution_api_server_url`) stay on loopback. The scheduler health check server stays disabled (the default). Tasks themselves make no network calls.
- The generated admin password stays in `workspace/airflow/` and is never committed or printed in reports.
- `scripts/airflow-up.sh` exits 78 if Airflow is not installed and cannot be installed (matching `scripts/lib/common.sh` exit codes).

Runs are triggered with `scripts/airflow-run.sh <scenario>`, which uses the
Airflow CLI with DAG run conf `{scenario, run_id, kit_root, fixed_clock, pass}`.
Scenario proofs use the real scheduler, not `airflow dags test`, so retries and
retry delays behave as they will in practice.

The scheduler never starts a DAG run, manual or scheduled, while the DAG is
paused; a triggered run stays `queued` (confirmed on 3.3.2). So
`airflow-run.sh` unpauses the DAG only for as long as its own runs take, then
restores the paused state, also on failure or interrupt (Decision 18). While
unpaused with `catchup=False`, the timetable only schedules future events, so
no backlog of 2026 runs is created. If a 20:00 Singapore business-day event
falls inside that window, the scheduler also starts that scheduled run with
the Decision 15 defaults; it writes only under `workspace/` and runs after or
before the scenario run because `max_active_runs=1`.

---

## 6. How we prove the Airflow version is equivalent

`scripts/test-airflow-equivalence.sh` runs all of these and exits 0 only if all
pass. Each check maps to an item in `BUILD_PROMPT.md` "What done means".

1. **DAG structure.** The DAG imports with no errors (DagBag). It has exactly 12 tasks with the canonical names and no cycles. Its edge set equals `graph_from_json`, `graph_from_xml`, and `graph_from_task_commands` edge sets (11 edges). `post_pending_ledger` has `retries=2`, `retry_delay=60s`, no exponential backoff; every other task has `retries=0`. Every task has the failure callback. The DAG is paused on creation, `max_active_runs=1`, `catchup=False`.
2. **Schedule.** Enumerate the DAG's timetable across 2026 and compare with the expected list built independently from calendars.json (`weekDays` minus `excludedDates`): 254 run dates, each at 20:00 in the decided timezone (Decision 2). Spot checks: first 2026-01-02, last 2026-12-31, no run on 2026-03-20 (Friday holiday), a run on 2026-03-19. Also check that calendars.json and the fixture calendar list the same holidays.
3. **Oracle.** For each of the five scenarios, `scripts/airflow-run.sh <scenario>` then `verify_run.py --run-dir <run> --kit-root .` passes every check. `runtime.json` and `run-manifest.json` say `mode: airflow` and `isControlM: false`.
4. **Cross-runtime equivalence.** For each scenario, run the harness and Airflow, then compare `verify_run.py --normalize` output. It must be identical. This covers exit sequence, counts, ledger rows per pass, input hashes, invariant results, every `stages/*.jsonl`, and the three JSON outputs (`verify_run.py:475-507`).
5. **Retry.** In `partial-ledger-write`: exit sequence shows `post_pending_ledger` attempt 1 exit 3, then attempt 2 exit 0; `ledger-posting.json` shows `alreadyPosted=4`, `inserted=3`; ledger has 7 rows and 7 distinct `payment_id`s. Airflow task instance history confirms `try_number` 2 for `post_pending_ledger` and 1 for the other 11 tasks. The two tries are at least 60 seconds apart.
6. **Whole-batch rerun.** In `duplicate-retry`: two DAG runs on one run directory, 24 exit entries with the second 12 identical to the first, `ledgerRowsAfterPass` equal to `[6, 6]`.
7. **No retry on other failures.** A DAG run triggered directly with a scenario id that does not exist fails at `watch_inbound_files` with one try, logs the failure callback, sends nothing, and leaves the other 11 tasks `upstream_failed`.
8. **Determinism.** For `happy-path`, `reconciliation-breaks`, and `partial-ledger-write`, two Airflow runs with the same run id produce byte-identical `stages/` and `output/` (mirrors `test-controlm-compatibility.sh:96-128`).
9. **No regression.** `./scripts/check.sh` still passes, including `./scripts/test-controlm-compatibility.sh` and every `repos/*/scripts/test.sh`.

Reporting rule: the harness and Airflow runs are both labelled for what they
are. Neither is Control-M, and passing `controlm-validate.sh` is not `ctm build`.

---

## 7. Risks

| Risk | Impact | Mitigation |
| --- | --- | --- |
| **The 2026 calendar runs out in under three months.** The DAG has no run dates after 2026-12-31, the same as Control-M with a 2026-only calendar. | The batch silently stops scheduling on 2027-01-01. | Out of scope to fix here. The runtime README states that a 2027 calendar must be added to calendars.json before year end. Raise with the calendar owner now. |
| **Unknown Control-M server timezone.** The definitions never state one. If the real server runs in UTC, 20:00 means 04:00 Singapore time. | Airflow could run 8 hours away from today's production time. | Decision 2. Confirm with the Control-M administrator before any real cutover. |
| **Representative calendar.** Three holidays fall on Sundays (2026-05-31, 2026-08-09, 2026-11-08) and no observed Monday is listed. The fixture says it is not an authoritative source (fixture calendar:45). | Real Singapore run dates would differ. | Fixtures are authoritative for this workshop and are not edited. Flag for production. |
| **Deliberate deviations from the literal definitions** (D6 attempt number, D7 retry trigger, D10 paths). | A reviewer comparing line by line will find differences. | Each one is recorded here and in a DECIDED decision before build. |
| **Airflow 3 API churn.** Imports, CLI, and config sections changed from Airflow 2 and are still moving across 3.x releases. | Build or test breakage on upgrade. | Exact version pin with constraints file; DAG import test in the equivalence suite. |
| **Shared filesystem assumption.** Tasks share state through the run directory. | Breaks under Celery or Kubernetes executors with separate workers. | LocalExecutor only. A production design would need a shared volume or object storage; out of scope. |
| **Manual runs may have no logical date in Airflow 3.** | Templates that use the logical date would fail or vary. | Nothing in the task command depends on the logical date. Run id and clock come from params. |
| **`try_number` rises when an operator clears a task.** | A manual clear sends `--attempt 2` or more, like `%%RUNCOUNT`. | Acceptable. Scenario proofs use fresh DAG runs; the whole-batch rerun uses a new DAG run (Decision 12). |
| **Changing `verify_run.py`.** | Any change risks weakening the oracle. | Only add `airflow` to the runtime-mode allow-list at `verify_run.py:132`. No other edit. The diff is reviewed in step 14. |
| **Slower checks.** Scheduler latency per task plus a 60 s retry delay. | The Airflow suite takes minutes, not seconds; `check.sh` is meant to take under a minute. | Keep the Airflow suite as its own command (Decision 16). |
| **Code quality gates.** Ruff lints the whole tree; mypy strict lists explicit paths; coverage is per repository. | The DAG may fail lint or be untyped. | DAG and scripts pass ruff. Add `runtimes/airflow` to mypy only if Airflow is installed in the dev venv; otherwise document the exclusion. |
| **Port 8080 conflict.** | Airflow fails to start. | Port is configurable in `airflow-up.sh`; still bound to 127.0.0.1. |
| **Mail routing intent is unclear** (D3). | Operations may expect the XML's per-team routing after cutover. | Decision 10; confirm with payments operations. |

---

## 8. Decisions

Each decision is OPEN until you answer it. `/migrate` builds only DECIDED
decisions.

All 18 decisions are DECIDED. Decisions 1 to 16 were decided on 2026-10-06 by
accepting each recommendation as written. Decisions 17 and 18 were added on
2026-10-06 when the build found two gaps (section 9) and were decided the same
day. No decision is OPEN.

### Decision 1: Which definition wins when the files disagree

- Options: (a) the JSON, as `graph.py:5-6` states; (b) the XML; (c) whichever is stricter, setting by setting.
- Recommendation: (a). The JSON is the declared authority and the only file whose command lines resolve (D2). Deliberate departures (Decisions 6, 7, 15) are listed explicitly.
- Status: DECIDED (a), 2026-10-06. Reason: the JSON is the declared authority (`graph.py:5-6`) and the only file whose command lines resolve (D2). Deliberate departures are Decisions 6, 7, and 15.

### Decision 2: DAG timezone

- Options: (a) `Asia/Singapore`; (b) UTC; (c) the Control-M server's local time, once someone confirms it.
- Recommendation: (a). It is the only timezone any file states (calendars.json:7, fixture calendar:47, every scenario's cutoff). Singapore has no daylight saving, so 20:00 is always 12:00 UTC.
- Status: DECIDED (a) `Asia/Singapore`, 2026-10-06. Reason: it is the only timezone any file states, and with no daylight saving 20:00 is always 12:00 UTC. Confirm with the Control-M administrator before any real cutover (section 7).

### Decision 3: How the schedule excludes holidays

- Options: (a) Airflow's built-in `EventsTimetable`, with the 254 run times built at parse time from calendars.json; (b) a custom timetable plugin that reads calendars.json; (c) a Monday-to-Friday cron that ignores holidays.
- Recommendation: (a). It needs no plugin, gives exactly the calendar's dates, and is easy to test. (c) fails "same 2026 run dates". (b) is more code for the same result while the calendar covers one year.
- Status: DECIDED (a) `EventsTimetable`, 2026-10-06. Reason: exactly the calendar's dates, no plugin, easy to test.

### Decision 4: `OrderMethod: Manual`

- Options: (a) keep the schedule and create the DAG paused; (b) no schedule at all (`schedule=None`), manual trigger only; (c) schedule and start unpaused.
- Recommendation: (a). Nothing runs until an operator acts, as with a manual order, and the schedule still expresses the `When` block so it can be tested. (b) is closer to the letter of `Manual` but makes the schedule check impossible.
- Status: DECIDED (a), 2026-10-06. Reason: nothing runs until an operator acts, and the schedule stays testable. How the scenario runner executes runs on a paused DAG is Decision 18.

### Decision 5: The 06:00 end of the window

- Options: (a) start at 20:00 and do not enforce an end, documented as a gap; (b) also log a warning (no mail) when a scheduled run is not finished by 06:00 the next day, using Airflow deadline alerts if the pinned version has them; (c) make tasks refuse to start after 06:00.
- Recommendation: (a), adding (b) only if the pinned version supports deadline alerts without extra code. (c) would break manual and test runs, whose fixed clocks fall outside the window (D13).
- Status: DECIDED (a) only, 2026-10-06. Reason: Airflow 3.3.2 has deadline alerts (`airflow.sdk.DeadlineAlert`), but each alert needs a callback function (`AsyncCallback` or `SyncCallback`), which is extra code, so the condition for adding (b) is not met. The 06:00 end is a documented gap.

### Decision 6: Which failures retry, and how fast

- Options: (a) CONTRACT semantics: only exit 3, only when the scenario declares that fault for that task and attempt, retries; every other failure fails at once; 60 s delay; (b) literal Control-M: any failure of `post_pending_ledger` retries up to 2 times, 60 s apart; (c) as (a) but with no delay, like the harness.
- Recommendation: (a). It matches the tested behaviour and `CONTRACT.md:156-157`. Retrying exit 4 (business rule) or 5 (missing input) cannot help. Keep the 60 s delay from the definitions; it does not change the exit sequence.
- Status: DECIDED (a), 2026-10-06. Reason: it matches `CONTRACT.md:156-157` and the tested behaviour, and keeps the definitions' 60 s delay.

### Decision 7: Where `--attempt` comes from

- Options: (a) Airflow `try_number`; (b) a static `1`, as the definitions literally say.
- Recommendation: (a). With (b), partial-ledger-write fails forever (D6). This is a recorded departure from the literal definition.
- Status: DECIDED (a) `try_number`, 2026-10-06. Reason: a static `1` makes partial-ledger-write fail forever (D6). Recorded departure from the literal definition.

### Decision 8: How each task runs the wrapper

- Options: (a) a Python task that runs the wrapper as a subprocess and implements section 5.2; (b) a BashOperator with the command line; (c) BashOperator plus a separate log-capture wrapper script.
- Recommendation: (a). (b) cannot tell exit 3 from other failures (needed for Decision 6), cannot write the per-attempt records, and treats exit 99 as "skipped" by default. (a) still calls the existing wrapper unchanged.
- Status: DECIDED (a), 2026-10-06. Reason: only a Python task can tell a declared exit 3 from other failures and write the per-attempt records; it still calls the existing wrapper unchanged.

### Decision 9: Tasks declared explicitly or derived from the definitions

- Options: (a) build the 12 tasks, edges, and retry limits at parse time from task-commands and `graph.py`, as the harness does (`harness.py:164-176`); (b) write the 12 tasks and edges out by hand in the DAG file.
- Recommendation: (a). The DAG cannot drift from the definitions, and `graph.py` refuses to load if the three files disagree. The structural test (section 6, check 1) still asserts the exact 12 names and 11 edges, so a reviewer gets the explicit list from the test.
- Status: DECIDED (a), 2026-10-06. Reason: the DAG cannot drift from the definitions, and the structural test still asserts the explicit 12 names and 11 edges.

### Decision 10: Failure notification

- Options: (a) a log-only `on_failure_callback` using the JSON's single recipient and message; (b) a log-only callback using the XML's per-job recipients, subjects, and urgency; (c) Airflow email with SMTP pointed nowhere.
- Recommendation: (a), because the JSON is the authority (Decision 1). Each log line names recipient, job, run id, and scenario. Ask payments operations whether the XML routing (D3) reflects what they want. (c) still attempts a network connection and breaks the no-network rule.
- Status: DECIDED (a), 2026-10-06. Reason: the JSON is the authority (Decision 1). Nothing is sent. Whether the XML's per-team routing (D3) is wanted stays a question for payments operations.

### Decision 11: How Airflow runs are graded

- Options: (a) `scripts/airflow-run.sh` writes `runtime.json` and builds `run-manifest.json` in the harness's format from `logs/airflow-attempts.jsonl`, reusing the harness module's helpers by import without editing it, and `verify_run.py` gains `airflow` in its runtime-mode allow-list (one line, `verify_run.py:132`); (b) a separate Airflow-only verifier; (c) the DAG writes the manifest from a DAG-level callback.
- Recommendation: (a). The oracle stays the same program with one allowed value added, which is the only `verify_run.py` change `BUILD_PROMPT.md` permits. (b) would duplicate and possibly weaken the oracle. (c) cannot cover the two-run `duplicate-retry` case cleanly. Scheduled production runs get per-attempt logs but no `run-manifest.json`, as with real Control-M today.
- Status: DECIDED (a), 2026-10-06. Reason: the oracle stays the same program. The only `verify_run.py` change is adding `airflow` to the runtime-mode allow-list at `verify_run.py:132`.

### Decision 12: Whole-batch rerun (`duplicate-retry`)

- Options: (a) a second DAG run on the same run directory with conf `pass=2`, tasks back at attempt 1; (b) clear the first DAG run and let it run again.
- Recommendation: (a). It mirrors `harness.py:532-533` and keeps attempt numbers at 1. (b) raises `try_number` to 2 and changes the exit sequence.
- Status: DECIDED (a), 2026-10-06. Reason: mirrors `harness.py:532-533` and keeps every task of the second pass at attempt 1.

### Decision 13: Where the DAG lives and how Airflow is installed

- Options for location: (a) `runtimes/airflow/`; (b) `repos/payments-orchestrator/airflow/`, which conflicts with its `STDLIB_ONLY` marker; (c) a new top-level `airflow/`.
- Options for install: (i) pinned venv under `workspace/airflow/` with the official constraints file and `airflow standalone` (replaced by Decision 17); (ii) the official Docker Compose stack bound to 127.0.0.1; (iii) a user-wide install.
- Exact version: the newest Airflow 3 patch release available at build time, written as an exact pin with its constraints file and a Python version that release supports.
- Recommendation: (a) with (i). It matches the `runtimes/control-m/` pattern, needs no Docker (AGENTS.md keeps Docker optional), and keeps everything generated in the git-ignored `workspace/`.
- Status: DECIDED (a) with (i), 2026-10-06, amended by Decision 17: the four Airflow components are started one by one instead of through `airflow standalone`. Exact version `apache-airflow==3.3.2` (the newest Airflow 3 release on 2026-10-06, published 2026-09-17), Python 3.12, installed with `https://raw.githubusercontent.com/apache/airflow/constraints-3.3.2/constraints-3.12.txt`.

### Decision 14: The `CRITICAL` flag

- Options: (a) record it in task docs only; (b) map critical jobs to a higher `priority_weight`; (c) let non-critical jobs (`classify_breaks`, `archive_and_notify`) fail without failing the run.
- Recommendation: (a). In a straight chain priority changes nothing. (c) would change behaviour: in Control-M a failed non-critical job still stops its successors because the condition is never added.
- Status: DECIDED (a), 2026-10-06. Reason: in a straight chain priority changes nothing, and (c) would change behaviour.

### Decision 15: Parameter defaults for runs without conf

- Options: (a) mirror the JSON defaults (`scenario=happy-path`, clock from `scenario.json`), derive paths from the repository root, and default `run_id` to `scheduled-<order date>`; (b) mirror the JSON literally, including `run_id=baseline-happy-path` and `/workspace` paths; (c) require conf and fail without it.
- Recommendation: (a). (b) writes outside the repository (D10) and reuses one run directory every day, which breaks "runs are immutable once complete" (`CONTRACT.md:163`). `airflow-run.sh` always passes explicit values, so tests do not depend on these defaults.
- Status: DECIDED (a), 2026-10-06. Reason: (b) writes outside the repository and reuses one run directory every day (D10).

### Decision 16: The one check command

- Options: (a) `scripts/test-airflow-equivalence.sh` runs every Airflow check, and `scripts/check.sh` calls it as an extra step that is skipped (exit 78) when Airflow is not installed; (b) make Airflow a required step of `check.sh`; (c) keep the two completely separate.
- Recommendation: (a). One command still runs everything when Airflow is present, and `check.sh` keeps working on machines without it.
- Status: DECIDED (a), 2026-10-06. Reason: one command runs everything when Airflow is installed, and `check.sh` keeps working on machines without it.

### Decision 17: How the local Airflow components start

- Found at build time: `airflow standalone` in 3.3.2 starts the scheduler and triggerer without `--skip-serve-logs`, and their log servers bind to all interfaces on ports 8793 and 8794 (`airflow/utils/serve_logs/core.py`, `host=""`). No setting changes that host, so `standalone` cannot meet "bound to 127.0.0.1 only".
- Options: (a) `scripts/airflow-up.sh` starts the same four components one by one: `scheduler --skip-serve-logs`, `dag-processor`, `api-server --host 127.0.0.1`, `triggerer --skip-serve-logs`, with LocalExecutor and SimpleAuthManager as `standalone` would force; (b) keep `airflow standalone` and accept the two extra listeners.
- Recommendation: (a). Checked on a throwaway 3.3.2 install: only `127.0.0.1:8080` listened, task logs stayed readable, retries worked.
- Status: DECIDED (a), 2026-10-06. Reason: it is the only option that keeps every Airflow listener on 127.0.0.1.

### Decision 18: Running scenarios on a DAG that is paused on creation

- Found at build time: the scheduler starts no DAG run, manual or scheduled, while the DAG is paused (Decision 4). A triggered run stays `queued`.
- Options: (a) `scripts/airflow-run.sh` unpauses the DAG only while its own runs execute, then restores the paused state, also on failure or interrupt; (b) leave the DAG unpaused after the first scenario run; (c) create the DAG unpaused, which reverses Decision 4.
- Recommendation: (a). The DAG is paused whenever no scenario is running, as with a `Manual` folder. Side effect: an event at 20:00 Singapore time on a business day that falls inside the window starts that scheduled run too (section 5.3).
- Status: DECIDED (a), 2026-10-06. Reason: it keeps Decision 4 and still lets scenario proofs use the real scheduler.

---

## 9. Changes after the first draft

| Date | Change | Why |
| --- | --- | --- |
| 2026-10-06 | Status changed from DRAFT to APPROVED; Decisions 1 to 16 marked DECIDED with their reasons. | The decision owner accepted every recommendation as written. |
| 2026-10-06 | Decision 5 recorded as (a) only. | The recommendation added (b) only if deadline alerts needed no extra code. In Airflow 3.3.2 every deadline alert needs a callback function. |
| 2026-10-06 | Decision 13 records the exact pin, `apache-airflow==3.3.2` on Python 3.12, and its constraints URL. | The decision deferred the exact version to build time. |
| 2026-10-06 | Section 5.3 and Decision 13 no longer use `airflow standalone`; added Decision 17. | `standalone` binds two log servers to all interfaces, which breaks the 127.0.0.1-only rule. Decided by the decision owner. |
| 2026-10-06 | Section 5.3 describes how `airflow-run.sh` runs a paused DAG; added Decision 18. | Triggered runs never start on a paused DAG, and the draft did not say how the runner handles that. Decided by the decision owner. |
| 2026-10-06 | Line references now say they match commit `7601515`, not `a5eefcb`. | The references match the harness and `verify_run.py` as refactored in `7601515` (for example the allow-list is `verify_run.py:132` there and line 113 at `a5eefcb`). |
