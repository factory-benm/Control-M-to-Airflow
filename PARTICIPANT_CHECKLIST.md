# Control-M to Airflow with Droid

You will take a small Control-M batch estate, make it ready for agents, write
a design doc, migrate it to Apache Airflow 3 with `/migrate`, and prove the
Airflow version behaves the same as the Control-M version.

The estate is fictional: an overnight cross-border payments reconciliation
batch with 12 Control-M jobs, one business calendar, and eight small Python
repositories under `repos/`. You do not need a Control-M licence. The scripts
fall back to a compatibility harness that runs the jobs exactly as the
Control-M definitions declare them.

## 1 / Before the session

- [ ] 1\. **Get Factory access.** Accept the workshop invitation with the email
  you will use for Factory. If nothing arrives, check spam, then ask the
  presenter.

- [ ] 2\. **Check your tools.** You need Git, Python 3.11 or newer, and Docker
  Desktop (running, with at least 4 GB of memory) for the Airflow part. Windows:
  use WSL with Ubuntu for everything below.

```sh
git --version
python3 --version
docker compose version
```

- [ ] 3\. **Install Droid.** Pick one:

```sh
# macOS or Linux
curl -fsSL https://app.factory.ai/cli | sh

# Homebrew (macOS)
brew install --cask droid
```

```powershell
# Windows PowerShell (then continue in WSL)
irm https://app.factory.ai/cli/windows | iex
```

See the [Droid CLI quickstart](https://docs.factory.ai/droid-cli/quickstart)
if anything goes wrong.

- [ ] 4\. **Clone the estate and run the legacy batch once.**

```sh
git clone https://github.com/factory-benm/Control-M-to-Airflow.git
cd Control-M-to-Airflow
git switch -c my-migration
./scripts/check-prereqs.sh
./scripts/controlm-validate.sh
./scripts/controlm-run.sh happy-path
./scripts/test-controlm-compatibility.sh
```

`controlm-run.sh` will say Control-M Workbench is unavailable and use the
compatibility harness. That is expected; Workbench needs BMC credentials.
**Done when:** the last command ends with
`the Control-M compatibility contract holds.`

- [ ] 5\. **Start Droid** in the same folder:

```sh
droid
```

Sign in with your invited email. Press `Ctrl+L` until autonomy shows
**Medium**, so Droid can edit files and run tests without asking each time.

- [ ] 6\. **Start the readiness report now.** It takes several minutes. Type
  this inside Droid, not in your shell:

```text
/readiness-report
```

**Done when:** Droid prints a readiness level and a list of action items.
Everyone's clone shares the same `origin`, so reports for this repository are
pooled in Factory.

## 2 / During the session

Rules for every step: read what Droid proposes before you approve it. If Droid
asks a question you cannot answer, reply `use your best judgment and stay within
the task`. After each step, run `git diff --stat` in a shell tab to see what
changed.

### Make the estate ready for agents

- [ ] 7\. **Read the readiness report.** Expect gaps such as no `AGENTS.md`, no
  CI, no single command that runs every test, and logging that differs per
  repository. Agents do much better work once these are fixed, so we fix them
  before migrating anything.

- [ ] 8\. **Add project instructions.** Paste into Droid:

```text
Inspect this estate and write a concise root-level AGENTS.md for coding agents. Cover what the batch does; the layout (repos/, the Control-M definitions in repos/payments-orchestrator/controlm/, fixtures/, the harness, scripts/); how to run and test it, using only commands that exist; the task exit codes; and safety rules: never edit fixtures or expected manifests to make a test pass, keep the three Control-M definition files in agreement, services stay standard-library only, tasks make no network calls, never push. Do not invent commands or conventions. Run every documented command that is safe to run and report the results.
```

**Done when:** `AGENTS.md` exists and Droid reports the commands it ran. Open
it and read it. Is anything wrong or made up?

- [ ] 9\. **Fix the build and test gaps.** Paste into Droid:

```text
/readiness-fix Focus on build, test, and validation gaps only. Add one root-level command that runs the prerequisite check, Control-M validation, every repository's tests, and the compatibility test, and document it in AGENTS.md. Do not change Control-M job definitions, task behaviour, fixtures, expected results, or the harness. Do not add Airflow yet. Run the new command when done.
```

**Done when:** the new command passes in your shell. Optional: run
`/readiness-report` again later to see how the level moved.

### Design before you migrate

- [ ] 10\. **Write the design doc.** Paste into Droid:

```text
Write docs/migration-design.md for moving this batch from Control-M to Apache Airflow 3. First read every Control-M definition file (JSON, XML, task-commands.json, calendars.json), the scenario fixtures, fixtures/CONTRACT.md, and the compatibility harness. Include: what Control-M does for this batch today; a mapping table with one row per Control-M job and setting, showing the Control-M value (file and line) and its Airflow equivalent; anything with no direct Airflow equivalent and how we will handle it; every place the definition files disagree with each other or with the fixtures; the target layout and local Airflow setup; how we will prove the Airflow version is equivalent; and risks. End with a Decisions section: one entry per choice I need to make, each with the options, your recommendation, and the status OPEN. Keep it to what this estate needs. Do not write any code.
```

**Done when:** `docs/migration-design.md` exists with a mapping table and a
Decisions section.

- [ ] 11\. **Review the design and make the decisions.** This is the most
  important review in the workshop, because `/migrate` builds exactly what this
  doc says. Read it and check that it gets these right:

  - 12 jobs in one straight line. Each Control-M condition becomes exactly one
    Airflow dependency.
  - Only `post_pending_ledger` retries: 2 reruns, 1 minute apart. Every other
    job has 0.
  - The task attempt number reaches the wrapper (`--attempt`). The
    partial-ledger-write fault only fires on attempt 1, so a retry that always
    sends `1` would fail forever.
  - Schedule: Monday to Friday, only on `PAYOPS_SG_BUSINESS_2026` business days,
    inside a 20:00 to 06:00 window that crosses midnight. The Control-M JSON
    never states a timezone; only `controlm/calendars.json` does. Which
    timezone does the DAG use, and why?
  - `OrderMethod: Manual` means Control-M does not order the folder
    automatically. Does the Airflow DAG start paused?
  - The failure mail becomes a logged callback, not a real email. The XML
    export has its own copy of the mail action on every job.
  - The result checker (`verify_run.py`) only accepts the Control-M harness and
    Workbench as runtimes. How will Airflow runs be graded without weakening it?

  Tell Droid what is wrong and give your answer to each open decision. Then
  paste:

```text
Update docs/migration-design.md with my corrections and answers. In the Decisions section, mark each answered decision DECIDED and record the reason. Then list any decision that is still OPEN. Do not write any code.
```

When nothing is OPEN, commit the doc and tag it. The tag lets you see later
whether `/migrate` changed the design:

```sh
git add docs/migration-design.md
git commit -m "docs: approve Airflow migration design"
git tag design-approved
```

**Done when:** every decision is DECIDED and the `design-approved` tag exists.

### Migrate

- [ ] 12\. **Decide how many `/migrate` runs you need.** For this estate, one.
  It is a single Control-M folder with 12 jobs, one calendar, and five test
  scenarios, which fits comfortably in one run.

  A real estate with hundreds of folders and thousands of jobs needs several
  runs, one per phase, each with its own goal and evidence: (1) inventory and
  pattern catalogue of the whole estate, (2) conversion tooling and mapping
  rules, (3) one pilot workflow certified end to end, (4) migration waves of
  folders grouped by pattern, (5) shadow runs and cutover. Each run starts from
  the previous run's outputs, and the design doc becomes the mapping rulebook
  that every run reuses.

- [ ] 13\. **Run `/migrate`.** Inside Droid, type `/migrate`, press Enter, and
  paste this prompt (the same text is in [BUILD_PROMPT.md](BUILD_PROMPT.md)):

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

What happens next:

1. Droid asks about anything the design doc does not answer. If it asks
   something the doc already decides, point it to the doc.
2. Droid proposes a **promise**: what will be true when the migration is done.
   Read it. Edit it if it misses something from the prompt or the design doc,
   then confirm.
3. Droid writes a plan and a validation strategy, then asks for **final plan
   approval**. Check that the plan builds what the design doc says and that
   every "done means" item has a check behind it.
4. Droid works until it is done, pausing only for decisions or blockers. If it
   finds a problem in the design, it stops and asks you. This is the longest
   step. Approve Docker commands when asked.

Droid keeps the promise, plan, and evidence outside the repository, under
`~/.factory/missions-m/`. The design doc is the record that stays in the
repository. **Done when:** Droid reports every item proven, or tells you
plainly which ones it could not prove.

### Check the result

- [ ] 14\. **Check the code matches the design.** Paste into Droid:

```text
Check the Airflow implementation against docs/migration-design.md and the Control-M definitions. For every row of the design doc's mapping table and every DECIDED decision, show the Control-M source (file and line), the Airflow code (file and line), and whether they match. List every mismatch, and anything the code does that the design doc does not mention. Also confirm that the JSON, XML, and task-commands.json still agree with each other. Do not change anything.
```

Then see whether the design changed after you approved it:

```sh
git diff design-approved -- docs/migration-design.md
```

**Done when:** there are no mismatches (or you understand and accept each
one), and every change to the design doc matches what Droid told you at the
end of step 13.

- [ ] 15\. **Test Airflow yourself.** Paste into Droid:

```text
Start the local Airflow environment and tell me the URL. Then show me the exact commands to trigger each of the five scenarios, to run every check, and to stop Airflow.
```

Open the Airflow UI at the URL Droid gives you, then:

- Graph view: 12 tasks in one line, no extra tasks.
- Trigger **happy-path**. All 12 tasks go green.
- Trigger **partial-ledger-write**. `post_pending_ledger` fails once with exit
  code 3, retries, and succeeds.
- Run the single check command from the end of step 13 in your shell. All five
  scenarios pass the oracle and match the legacy output.

Expected results (from `fixtures/scenarios/*/expected/manifest.json`):

| Scenario | Received | Rejected | Duplicates | Cutoff adjusted | Posted | Matched | Breaks |
| --- | --- | --- | --- | --- | --- | --- | --- |
| happy-path | 6 | 0 | 0 | 0 | 6 | 6 | 0 |
| duplicate-retry (batch runs twice) | 8 | 0 | 2 | 0 | 6 | 6 | 0 |
| business-cutoff | 6 | 0 | 0 | 3 | 6 | 6 | 0 |
| partial-ledger-write (retry) | 7 | 0 | 0 | 0 | 7 | 7 | 0 |
| reconciliation-breaks | 11 | 2 | 0 | 0 | 9 | 5 | 4 |

**Done when:** every scenario matches this table in Airflow, and the check
command passes.

- [ ] 16\. **Optional: prove the checks catch drift.** In the Airflow DAG, set
  retries on `post_pending_ledger` to 0, run the check command, and watch
  partial-ledger-write fail. Then undo your edit and run the check again to
  see it pass.

## 3 / Droid keyboard shortcuts

Use these inside Droid's input, not your shell. On Mac, `Ctrl` means Control.

| Action | Keys / command |
| --- | --- |
| Switch Normal and Spec mode | `Shift+Tab` |
| Cycle autonomy: Off, Low, Medium, High | `Ctrl+L` |
| Change model | `Ctrl+N`, or type `/model` |
| Add a new line without sending | `Shift+Enter`; run `/terminal-setup` if needed |
| Interrupt Droid | `Ctrl+C` once; twice quickly exits |
| Show detailed tool output | `Ctrl+O` |
| Show all shortcuts | `?` with an empty input |
| Run a shell command inside Droid | `!` on an empty input; `Esc` returns to chat |

Workshop default autonomy: **Medium**. High is not needed. Never push, merge,
or deploy during the workshop.

## 4 / If you get stuck

| Problem | What to do |
| --- | --- |
| `droid` not found after install | Close and reopen Terminal, `cd Control-M-to-Airflow`, retry. |
| `check-prereqs.sh` says Python is too old | Install Python 3.11 or newer (for example `brew install python@3.12`) and reopen Terminal. |
| `controlm-run.sh` says Workbench is unavailable | Expected. The compatibility harness runs instead. |
| Docker is not running | Start Docker Desktop and wait until it says it is running. Give it at least 4 GB of memory. |
| Port 8080 is already in use | Ask Droid to move the Airflow web server to another local port. |
| `/readiness-fix` says no report found | Run `/readiness-report` first (step 6) and let it finish. |
| `git commit` asks who you are | Run `git config user.name "Your Name"` and `git config user.email you@example.com` in this folder, then commit again. |
| Droid closed in the middle of `/migrate` | Run `droid --resume --last` in the same folder and ask it to continue. |
| Droid did something you did not want | Inspect with `git diff`. Restore a single file with `git checkout -- <file>`. Do not reset the whole tree. |
| Way behind | Clone the repository again into a **new** folder and start from step 4. Never delete your own folder. |
