# ADR0005: Z.AI GLM Coding Plan as a second subscription provider

Date: 2026-10-08. Status: implemented on branch `rs1-glm-coding-plan`.

## Decision

Add `zai_coding_plan` as a provider in its own right, mirroring how `minimax`
is built end to end (accounts, credential resolution, probe, catalogue,
bands, profiles, doctor, docs). OpenCode serves it through the
`zai-coding-plan/` model prefix on the GLM Coding Plan (Max).

Four owner decisions made during implementation, in order:

1. Economical GLM (2026-10-08): routine work defaults to the cheap models
   (`glm-5.3-flash` first), and premium models (`glm-5.3`, `glm-5.2`) carry a
   higher per-dispatch cost estimate.
2. Peak pricing (2026-10-08): Z.AI charges 50% of the standard credit rate
   off-peak, with peak being Monday to Friday 14:00-18:00 Singapore time. A
   config-driven `peak_windows` table blocks premium GLM during peak and
   halves their estimated cost off-peak.
3. Codex for difficult work and GLM limited to routine work (2026-10-08,
   final config state): `codex:gpt-5.6-luna`, `codex:gpt-5.6-terra` and
   `codex:gpt-6-sol` are released from the 2026-09-28 override for design,
   diagnosis and high stakes. The order is luna, terra, then sol for a
   top-band first attempt or after luna and terra fail; astra stays forbidden
   per #32. `zai_coding_plan:glm-5.3-flash` remains available for routine work
   only and never qualifies for difficult tasks.
4. Claude fallback above reserve (2026-10-08): difficult work falls back to
   `claude:claude-sonnet-5-5`, then `claude:claude-opus-5-5` after a Sonnet
   retry fails, then blocks with nothing eligible. Opus is retry-eligible only
   after Sonnet for design, diagnosis and high-stakes work. Claude's
   30-percent reserve remains enforced, and `claude:claude-haiku-4-5` remains
   forbidden.

Decisions 3 and 4 are what config.json currently enforce. The peak-window and
premium-cost machinery from decisions 1 and 2 remains in place and dormant:
removing the premium GLM entries from `model_policy.forbidden` re-activates
premium routing with the peak gate and the off-peak half-cost estimate,
without any code change.

## The probe reports unmetered, honestly

The GLM Coding Plan meters 5-hour and weekly credits but publishes no quota
API. Checked on 2026-10-08, none of these expose one:

- docs.z.ai (overview, quick start, FAQ; all point at the web console),
- docs.z.ai/openapi.json (model APIs only),
- the models.dev catalogue entry,
- Z.AI's own `@z_ai/coding-helper` npm package (no quota endpoint strings),
- live GETs against every monitor/quota/usage path shape under api.z.ai
  (404 or an internal `404 NOT_FOUND`).

So `probe_zai_coding_plan` confirms credential presence, but does not verify
that the credential is accepted by a live endpoint, and returns
`{"status": "ok", "unmetered": true}` with no buckets and no invented
numbers. Policy treats an unmetered plan as eligible in every band, sorted
behind the metered plans, which is exactly the owner's spreading rule: when
MiniMax has headroom MiniMax is preferred, and when it is below reserve the
GLM plan takes the work.

The credential itself lives in OpenCode 2.x's own sqlite store
(`opencode.db`, table `credential`, `integration_id = 'zai-coding-plan'`),
not `auth.json`. `secret()` reads it read-only via a URI connection, and the
value never leaves the process except as the return value.

## What the plan can serve is recorded, not assumed

The catalogue (models.dev) advertises seven GLM ids; this Max plan refuses
the two highspeed ones ("your current subscription plan does not yet include
access", verified live by dispatch and by the plan's own `/models` list).
`rightsize refresh` now diffs the catalogue against
`https://api.z.ai/api/coding/paas/v4/models` and records
`plan_available` / `plan_refused` per provider, and `doctor` errors on any
ladder entry the plan refuses.

## Effort wiring is partly verified, but built-in coding-plan paths are not

OpenCode 2.0.20 has no generic effort flag for these providers, but per-model
options passed through on generic providers in a local echo test:
`provider.<id>.models.<model>.options.reasoningEffort` was observed as body
`reasoning_effort` on an OpenAI-compatible provider, and
`options.thinking` / `options.output_config` passed through on an Anthropic
provider. The built-in `zai-coding-plan` and `minimax-coding-plan` providers
were not wire-verified, so these options remain unverified there. Evaluate
OpenCode's native `-m provider/model#variant` form separately. Z.AI documents `reasoning_effort` with only
`low`/`high`/`max` for GLM-5.3 family models; MiniMax's anthropic-compatible
Messages API documents `output_config.effort` (low..max) honoured by
`MiniMax-M3.1-Flash-Preview` only.

`effort_options` in config.json maps levels to those options; the launcher
templates carry the rendered document (`__RIGHTSIZE_MODEL_OPTIONS__` replaced
post-format by `launch_command`): the orca launcher writes any non-empty
options file under RightSize's state directory, outside the new worktree, and
starts opencode with `OPENCODE_CONFIG` pointing at it; the shell launcher
inlines `OPENCODE_CONFIG_CONTENT`. A repo's own `opencode.json` is never
clobbered. MiniMax emits no effort options until its built-in path is verified.

## Sources

- https://docs.z.ai/devpack/overview (models, credit windows, off-peak 50%,
  peak hours Mon-Fri 14:00-18:00 UTC+8)
- https://docs.z.ai/openapi.json (`reasoning_effort`, `thinking`)
- https://platform.minimax.io/docs/api-reference/text-chat-anthropic
  (MiniMax thinking types, `output_config.effort`, per-model support)
- https://models.opencode.ai/api.json (`zai-coding-plan` catalogue entry)
- https://api.z.ai/api/coding/paas/v4/models (live plan model list)
- OpenCode wire verification 2026-10-08 (echo endpoint, this worktree,
  `.scratch/effort-wire`)
