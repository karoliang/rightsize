# MiniMax Ultra and Orca

MiniMax Ultra is a subscription tier. Rightsize selects `minimax:MiniMax-M3`
in band 2 using direct Token Plan quota, independently of `opencode:minimax-m3`.

## Installed coding agent

On this machine, `mcode` 0.5.0 and `mmx` 1.0.26 are installed globally through
npm. MiniMax Code has been signed in through its global browser login. A
bounded, tool-disabled request returned `OK` with native model evidence
`MiniMax-M3`, `minimax-managed`. The MCode TUI also started successfully in an
Orca terminal. No repository task was dispatched.

Launch in a project terminal with:

```sh
mcode --model minimax/MiniMax-M3
```

Or open an Orca terminal in the current workspace:

```sh
orca terminal create --worktree active --title 'MiniMax M3' \
  --command 'mcode --model minimax/MiniMax-M3' --json
```

This is the supported terminal path, not a claim that Orca exposes a built-in
MiniMax agent picker. MCode's native login is separate from `mmx` and OpenCode.

## Enable automatic quota-aware selection

Rightsize's OpenCode adapter needs a **Token Plan subscription key**. In
OpenCode, use `/connect`, select **MiniMax Coding Plan**, and enter that key
locally. Do not use a pay-as-you-go key. Rightsize reads only the native
`minimax-coding-plan` entry, never the separate `minimax` entry.

`MINIMAX_API_KEY` is also supported through the existing project-scoped
Infisical configuration or environment. An existing vault binding is
authoritative: a missing vault key does not silently use native credentials.
For an intentional native binding, merge these entries into local Rightsize
configuration, preserving existing bindings:

```json
{
  "accounts": {"minimax": "minimax-local"},
  "account_bindings": {
    "minimax-local": {"runtime": "opencode", "home": "/Users/karo/.local/share"}
  }
}
```

Then run `rightsize probe` and `rightsize models`. A valid probe must
report live rolling and weekly windows; absent credentials or incomplete
responses leave MiniMax unavailable. `rightsize refresh` includes the direct
MiniMax catalog. Existing account-bound launcher restrictions still apply:
scoped-vault dispatch and Orca account binding require a managed adapter.

Validation on 2026-09-21: the user connected the subscription key through
OpenCode. config.json now explicitly selects minimax-local with the native
~/.local/share home. The live quota endpoint reported 99% remaining in both
windows; Rightsize selected MiniMax-M3 with 84 percentage points after its
15% reserve. A tool-denied OpenCode smoke through
minimax-coding-plan/MiniMax-M3 returned OK and exited0. The29 focused
MiniMax/credential/account tests passed. MCode OAuth remains separate.

See [decision and sources](decisions/0002-minimax-ultra.md).
