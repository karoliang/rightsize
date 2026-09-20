#!/usr/bin/env python3
"""Measure whether a question actually separates good input from bad.

A question that never fires looks exactly like a question with nothing to
report, so a judgment is not trustworthy until it has been shown to answer
differently on cases you already know differ, by a margin rather than merely in
the right order.

    ./eval_questions.py                 # every question, against the fixtures below
    ./eval_questions.py spec_complete   # one question

Needs TYPESAFE_API_KEY, so run it through the `rightsize` wrapper or export the
key yourself. Costs a fraction of a cent: input is a sentence per call.
"""

import sys

import rightsize as rs

# Each fixture is (label, spec, expected). `expected` is the answer a correct
# question should give: for a noul, roughly high or low; for the choice, the
# tier; for the score, roughly where on the scale.
FIXTURES = [
    (
        "vague",
        "fix the invoices thing",
        # No tier or size expectation on purpose: "invoices" is money-adjacent and the
        # scope is unknown, so any label here would be asserting something indefensible.
        {"spec_complete": "low"},
    ),
    (
        "context-dependent",
        "make the change we discussed to the invoices endpoint",
        {"spec_complete": "low"},
    ),
    (
        "medium",
        "add pagination to the invoices list endpoint, existing tests cover the handler",
        {"spec_complete": "high", "tier": "implementation"},
    ),
    (
        "detailed",
        "In litehq.com, add cursor pagination to GET /api/invoices in "
        "app/api/invoices/route.ts, default page size 50, accept a cursor query param, "
        "keep the existing response shape and make sure the handler tests in "
        "__tests__/api/invoices.test.ts still pass",
        {"spec_complete": "high", "tier": "implementation", "second_opinion": "high"},
    ),
    (
        "typo",
        "fix the spelling of 'recieve' in the comment at the top of lib/mailer.ts",
        {"tier": "mechanical", "size": "low", "destructive": "low", "second_opinion": "low"},
    ),
    (
        "migration",
        "drop the legacy invoices_v1 table and its foreign keys in a Supabase migration, "
        "after confirming nothing reads it",
        {"tier": "high_stakes", "destructive": "high"},
    ),
    (
        "architecture",
        "decide how background jobs should be scheduled across our Netlify functions and "
        "Supabase, and write it up",
        {"tier": "design", "second_opinion": "high"},
    ),
    (
        "repo-wide",
        "replace every direct process.env read across the web app, the edge functions and "
        "the shared package with the typed config module, and delete the old helper",
        {"size": "high", "tier": "implementation"},
    ),
    (
        "no-repro",
        "users occasionally see an empty invoices list on first load, nobody has reproduced "
        "it locally, find out why",
        {"tier": "diagnosis"},
    ),
]

MARGIN = 0.35


def answer(question_id: str, spec: str, key: str):
    data = rs.post(
        rs.TYPESAFE_URL,
        key,
        {"state": spec, "model": "jev-latest", "questions": {question_id: rs.QUESTIONS[question_id]}},
    )
    result = data["answers"][question_id]
    return result.get("noul", result.get("score", result.get("choice")))


def main(argv):
    key = rs.secret("TYPESAFE_API_KEY")
    if not key:
        print("TYPESAFE_API_KEY not available", file=sys.stderr)
        return 2
    wanted = argv[1:] or list(rs.QUESTIONS)
    failures = 0
    for question_id in wanted:
        print(f"\n# {question_id}")
        highs, lows = [], []
        for label, spec, expected in FIXTURES:
            if question_id not in expected:
                continue
            value = answer(question_id, spec, key)
            want = expected[question_id]
            print(f"  {label:<18} {value if isinstance(value, str) else f'{value:.2f}':<8} expected {want}")
            if isinstance(value, str):
                if value != want:
                    print(f"    MISMATCH: wanted {want}")
                    failures += 1
            elif want == "high":
                highs.append((label, value))
            elif want == "low":
                lows.append((label, value))
        if highs and lows:
            worst_high = min(highs, key=lambda row: row[1])
            best_low = max(lows, key=lambda row: row[1])
            gap = worst_high[1] - best_low[1]
            verdict = "ok" if gap >= MARGIN else f"TOO NARROW, want {MARGIN}"
            print(
                f"  margin {gap:.2f} between {worst_high[0]} ({worst_high[1]:.2f}) and "
                f"{best_low[0]} ({best_low[1]:.2f}): {verdict}"
            )
            if gap < MARGIN:
                failures += 1
    print("\nfailures:", failures)
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
