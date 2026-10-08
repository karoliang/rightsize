# Z.AI GLM Coding Plan

The GLM Coding Plan (Max) is a subscription, like MiniMax Ultra, and is
routed as its own provider `zai_coding_plan` with the `zai-coding-plan/`
model prefix through OpenCode. See [decision and sources](decisions/0005-glm-coding-plan.md).

## Current policy shape (2026-10-08)

- Routine work: MiniMax first; GLM `glm-5.3-flash` when MiniMax is below
  reserve (unmetered, offered in every band behind the metered plans).
- Difficult work (design, diagnosis, high stakes): `gpt-5.6-luna`, then
  `gpt-5.6-terra`; `gpt-6-sol` is eligible for the top band or after luna and
  terra fail. The Claude fallback is `claude-sonnet-5-5`, then
  `claude-opus-5-5` for a retry after Sonnet, then nothing eligible. GLM
  `glm-5.3-flash` is routine-only and never qualifies for difficult work.
  Astra stays forbidden (#32).
- Every GLM model except `glm-5.3-flash` is on the forbidden list ("GLM
  limited to glm-5.3-flash"). The premium models keep their profiles, the
  higher `dispatch_cost_models` estimate and the `peak_windows` pricing gate,
  so lifting the forbidden entries re-activates them with peak blocking and
  the off-peak half-cost estimate and nothing else changes.

## Credentials

Log in with `opencode auth login` and pick **Z.AI Coding Plan**. OpenCode
2.x stores that credential in its own database
(`~/.local/share/opencode/opencode.db`, table `credential`,
`integration_id = 'zai-coding-plan'`), not in `auth.json`; rightsize reads
it read-only from there, or from a `zai-coding-plan` entry in `auth.json`,
or `ZAI_CODING_PLAN_API_KEY` in the environment, in that order. The key is
never printed, logged or committed; only a digest identifies the account.

## Headroom

The plan meters 5-hour and weekly credits but publishes no quota API (checked
2026-10-08: docs.z.ai, the OpenAPI spec, models.dev, Z.AI's coding-helper
package, and live probing of plausible endpoints; see the ADR). The probe
therefore reports the plan as `unmetered` with no buckets: eligible wherever
the credential exists, sorted behind every metered plan. When Z.AI publishes
a usage endpoint, replace the probe body the way `probe_minimax` reads
`/v1/token_plan/remains` and map the 5-hour window to `rolling` and the
weekly window to `weekly`.

## What the plan serves

`rightsize refresh` records `plan_available` and `plan_refused` for the
provider by diffing the models.dev catalogue against the plan's own model
list (`https://api.z.ai/api/coding/paas/v4/models`). Today the Max plan
refuses `glm-5.3-highspeed` and `glm-5.2-highspeed`; they stay off every
ladder, and `doctor` errors if a refused id is laddered.

## Effort

`glm-5.3-flash` documents `low`/`high`/`max` (Z.AI's documented levels), and
generic OpenCode provider wiring was observed with per-model options. The
built-in `zai-coding-plan` path is not wire-verified, so the launcher claim is
unverified. Evaluate OpenCode's native `-m zai-coding-plan/glm-5.3-flash#<variant>`
form separately. The built-in MiniMax path is also unverified, so MiniMax
effort options are emitted as nothing until that path is tested.
