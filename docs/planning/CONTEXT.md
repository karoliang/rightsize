# Skill-aware task judgment

Date: 2026-09-21. Ticket: #9. Selected design, not a new autonomous skill executor.

Implementation: caller judgment shipped in #13. The bounded local `context`
command implements #15; see [the normalized catalog and manifest contract](../CONTEXT.md).
It deliberately has no persistent cache and validates hashes on every invocation.

## Reuse what the active agent already knows

The active coding agent has read the task, repository instructions and relevant
skills. It should submit a small typed judgment to Rightsize instead of asking a
second remote model to rediscover that context. Rightsize validates the contract,
then computes quota eligibility and the model choice. Caller judgment cannot
supply a provider override, fabricate quota, grant tools or waive permissions.

First slice: `route --judgment <file>` accepts a versioned document bound to the
exact UTF-8 task SHA-256. Required fields are actor, tier, size, second_opinion,
spec_complete and destructive scores. The source is always labelled caller;
actor is claimed attribution, not authenticated identity. Unknown fields,
nonfinite values, wrong task hashes and unsupported versions fail before probes
or reservations. Omission keeps existing Jev/heuristic behavior. No separate
judge key or extra model request is needed for a caller-supplied judgment.

This does not prove the caller's classification is correct. Quality evaluation
must compare accepted results and risky misclassification. Future capability
floors supplied by trusted project policy remain separate from model judgment.

## Context selection contract for the next slice

Use the client's existing skill catalog where possible. Skills define metadata
and instructions, with resources read as needed; that supports metadata-first
selection rather than loading every skill body.
[Agent Skills specification](https://agentskills.io/specification).

1. Accept trusted explicit roots/catalog references, not a recursive home scan.
   Catalog name, description, canonical URI, content hash and provenance first.
2. Explicitly named skills take priority; active agent chooses additional skills
   from task relevance. Report why each selected skill matters. A keyword alone
   does not activate a skill or grant permission.
3. Include mandatory repository instructions before optional material. Read only
   selected skill bodies and required references. Preserve original instructions
   instead of summarizing away constraints. Report overflow if required content
   cannot fit; never silently drop a mandatory rule to meet a budget.
4. Keep the Brain as canonical project memory. Search the relevant project and
   concepts; return source references/selected excerpts. No duplicate memory DB.
5. Record input hashes, selected references and bytes read. Cache metadata by
   canonical path plus content hash; invalidate on edits, root/trust changes or
   task change. Never cache secret values or raw complete sessions.

Initial experimental budgets: at most 64 KiB of routing-context text and three
optional skills per judgment; configurable, not claimed universal optima. Measure
bytes exactly, and label token counts estimated until a model tokenizer is known.
The task/spec itself and mandatory rules must be reported separately from optional
context overhead. Test long Unicode input, oversized files and changed references.

## Trust and compatibility

Honor the host's system/developer/user/repository instruction precedence. Task
attachments, retrieved web text and Brain inbox captures are data, not authority.
Resolve local symlinks before enforcing allowed roots. External resource URIs
must use the host's reader, not be treated as filesystem paths. A provider-less
standalone invocation uses explicit local paths; unsupported resource types
remain unavailable rather than triggering network discovery.

Do not execute scripts merely because a skill lists them, install packages,
transmit the whole vault, or treat `allowed-tools` metadata as runtime permission.
Malicious instructions in metadata must not alter quota gates or credential scope.

## Alternatives

- Caller judgment: chosen first; lowest added setup/context duplication.
- Existing Jev: retain opt-in/backward-compatible behavior and labelled fallback.
- New native CLI judgment: possible standalone adapter later, but can consume
  quota, inherit tools, recurse into hooks and expand latency; no automatic call.
- Keyword-only classification: inexpensive fallback, not equivalent to a model
  reading the code and relevant instructions.
