# Contributing

Contributions are welcome, particularly new providers, new launchers, and
better question wording. This file says what the project will and will not
accept, so nobody spends an evening on a PR that was never going to land.

## What this project is

A tool that decides which plan and model should run a task, then steps out. It
is never in the token path: no proxy, no gateway, no request forwarding. If a
feature would put rightsize between a caller and a model, it belongs in a
different project.

## Design rules

These are the constraints, not preferences:

1. **Python 3 standard library only.** This runs before every dispatch and
   inside a launchd job. A dependency that fails to import turns a routing
   helper into a broken run. No `requests`, no `pydantic`, no framework.
2. **Quota is arithmetic, the task is a judgment.** Numbers stay in code.
   Classification comes from a validated caller judgment or the existing optional model judge. The model is never asked which provider to
   use: providers change every few months, the questions do not.
3. **An estimate must never look like a measurement.** Every bucket carries a
   `source`, and unknown headroom (`percent: None`) is offered only for work
   that already earned an escalation. This is the rule most easily broken by
   accident.
4. **Config over code.** Reserves, ladders, thresholds, launchers and model
   prefixes live in `config.json`. If your change needs a code edit to tune a
   number, make the number a config key.
5. **Advisory, never blocking.** Hooks and integrations print advice and exit 0.
   A router that can stop a dispatch can strand a run.
6. **Every rule states its reason.** Output lines explain themselves
   (`chosen: its weekly bucket resets in 21h with 9 points usable`). A decision
   nobody can audit is not much better than a guess.

## Running things

```bash
python3 test_rightsize.py    # the policy, offline, no network, no tokens
python3 test_reliability.py  # quota, launcher, receipt and audit regressions
python3 test_judgment.py     # caller contract, no separate model call
python3 hooks/test_hook.py   # hook matching
./eval_questions.py          # the judgments, needs TYPESAFE_API_KEY, costs ~a cent
python3 -m compileall -q .   # syntax over every file
```

`test_rightsize.py` must stay offline and hermetic: it points `rightsize.STATE`
at a temp file so a contributor's real quota snapshots cannot change the result.
Keep it that way.

## Adding things

- **A launcher** (Aider, Cursor, a CI job, your own dispatcher): config only.
  See [docs/ADAPTERS.md](docs/ADAPTERS.md).
- **A provider** (a plan with its own quota): one probe function plus config
  entries plus a test case with a synthetic probe. The three rules a probe must
  respect are in the same file.
- **A model on a ladder**: `config.json` only, but say in the PR why that band.
  Band 1 is the default because price is the burn multiplier on a percent-bucket
  plan; a model that eats a weekly allowance twenty times faster is not a band 1
  workhorse however good it is.

## Changing a question

The five questions in `QUESTIONS` are the judgment. Changing one needs
evidence, not an argument:

1. Add or adjust fixtures in `eval_questions.py`, including cases you expect to
   fall on each side.
2. Run `./eval_questions.py <question_id>` before and after.
3. Put both margins in the PR description.

A question must **separate**, not merely order. The bar is a margin of 0.35
between the worst "high" fixture and the best "low" one. A question that scores
every fixture between 0.02 and 0.11 is ordering them correctly and
discriminating nothing, which looks exactly like a question with nothing to
report. That happened to `spec_complete`; the story is in
[docs/POLICY.md](docs/POLICY.md).

When a fixture and the model disagree, consider that the fixture may be wrong.
Two of ours were, and were deleted rather than argued with.

## Pull requests

- One change per PR. A new provider and a policy change are two PRs.
- Say what you measured. "Feels better" is not a result; a before/after margin,
  a timing, or a reproduced bad route is.
- Update the docs in the same PR: `README.md` for anything user-facing,
  `docs/POLICY.md` for a rule change, `docs/ADAPTERS.md` for a seam,
  `CHANGELOG.md` always.
- No secrets, ever. `registry.json`, `daily.log` and `.infisical.json` are
  gitignored for a reason; see [SECURITY.md](SECURITY.md).

Prose style in this repo: no em-dashes, absolute ISO dates, and comments that
say why rather than what.

## Filing issues

A useful routing bug report has the decision in it:

```bash
rightsize route --task "..." --json    # redact nothing; it contains no secrets
rightsize probe                        # the state it decided from
```

Say what you expected instead, and which rule you think it broke.
