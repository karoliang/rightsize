# Credential and account contract

Date: 2026-09-21. Ticket: #8. This is a design inventory, not credential migration.

## Evidence and supported reuse

| Runtime/source | Supported reuse | Proposed Rightsize behavior |
| --- | --- | --- |
| Codex native login | ChatGPT subscription and API-key login are distinct modes; native caching/refresh owns session credentials | Run the installed CLI/app-server under the selected account home; ask for account/rate-limit metadata, not tokens |
| Claude Code native login | Native sign-in, environment credentials and API-key helpers have their own precedence | Launch the unmodified binary in its existing user context; inspect supported status/events; do not extract OAuth tokens |
| OpenCode | CLI documents its provider auth store, environment keys and explicit provider/model selection | Let the CLI use its configured auth; retain only the narrowly scoped existing quota adapter until a native quota interface is established |
| Explicit API key | An API key authenticates its named provider/account, not a subscription interchangeably | Pass through an explicit allowlisted environment binding; do not copy it into state or issue output |
| Existing Infisical link | CLI can get named secrets with explicit project, environment and path | Opt-in, exact named read; no recursive export, imported-secret traversal or default-environment guessing |
| Knowledge vault | Repository rules and project knowledge, not provider authentication | Read selected approved notes as context; never scan it for keys |

Codex's current documentation distinguishes subscription and API billing, and
app-server documents account/rate-limit methods. Native authentication is the
supported boundary chosen here; these pages do not establish arbitrary API-token
reuse by a generic gateway. [Codex auth](https://learn.chatgpt.com/docs/auth),
[app-server](https://learn.chatgpt.com/docs/app-server).

Claude documents native authentication and JSON session output. Its credential
rules distinguish native end-user sign-in from third-party credential
intermediation. Therefore this design launches native Claude Code, or uses an
explicit API credential for an API integration; it does not create a new Claude
OAuth login or extract native tokens. [Authentication](https://code.claude.com/docs/en/authentication),
[programmatic use](https://code.claude.com/docs/en/headless),
[credential rules](https://code.claude.com/docs/en/legal-and-compliance).

OpenCode documents provider credentials in its auth file and environment and
`run --format json`. This is evidence for native reuse, not proof every stored
provider credential can be exported into another client. [CLI](https://opencode.ai/docs/cli/).

Infisical documents named secret reads and an environment default of `dev`.
Use an explicitly chosen environment for a router integration.
[Secret commands](https://infisical.com/docs/cli/commands/secrets).

## Resolution and failure behavior

1. Explicit account/credential reference on the invocation wins. It names a
   source, never contains the secret in persisted routing data.
2. Trusted project binding overrides the user's selected default binding.
3. Otherwise use the native runtime's current account/profile. Multiple accounts
   without a selected binding are ambiguous; do not pick the newest file.
4. A vault reference is consulted only when configured, with exact project,
   environment, path and name. No global fallback search and no silent switch to
   API billing after subscription failure.

Within a native runtime, preserve its documented credential precedence; Rightsize
must not pretend its own ordering overrides the runtime's environment/config.
Detect conflicting explicit/native contexts and report the ambiguity. Account
metadata must match quota and launch. Key changes or native account switches
invalidate cached quota and reservations for new admissions. Existing attempts
retain their original account binding until reconciled.

Credentials stay in the owning runtime or vault; refresh stays there too. An
expired login is `reauth-required`, an unavailable probe is `unknown`, an explicit
quota denial is `denied`. Do not turn any of these into another billable account.

## Proposed doctor output

Only runtime version, source kind, opaque account reference, selected/not-selected,
quota quality and next action. Example: `codex native selected; quota live` or
`vault binding incomplete: environment required`. No values, tokens, emails,
raw provider errors, full auth-file dumps or authentication mutation in doctor.
A binary's presence is not proof of a valid login; expose `unverified` until a
supported status probe succeeds.

## Local inspection and implementation gaps

Read-only help checked: Codex0.155.1, Claude Code2.1.267, OpenCode1.18.31.
Codex exposes JSON events and an output schema; OpenCode exposes JSON events.
No model invocation or new login was needed for this inventory.

Existing `rightsize` shell entry point exports the linked Infisical project into
the process. `secret()` also has a named lookup without explicit environment.
These are current behaviors to replace with scoped bindings, not evidence the
new contract already exists. Existing Codex probing uses native app-server but
state remains predominantly provider-scoped, not account-scoped. A blocking
`readline()` can outlive its nominal timeout: the native adapter needs a real
deadline and child reaping before it is a reliable discovery mechanism.
