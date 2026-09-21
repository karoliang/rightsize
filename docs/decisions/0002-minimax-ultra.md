# ADR0002: MiniMax Ultra is a separate subscription pool

Date: 2026-09-21. Tracking: [#23](https://github.com/karoliang/rightsize/issues/23), parent #20.

User decision: add the new MiniMax Ultra subscription to model selection and
install MiniMax tooling for Orca. Implementation choice: put direct
`minimax:MiniMax-M3` in band 2, alongside the existing Go-hosted M3. Ultra is a
plan tier, not a model identifier. Keep the two providers' quota independent.

Use the official global Token Plan endpoint and explicit remaining percentages
for both rolling and weekly windows. The official CLI documents ambiguous
legacy count semantics, so counts alone cannot establish spendable capacity.
Ignore media pools, reject expired/incomplete windows, respect exhaustion, and
retain a 15% reserve. Six in-flight tasks is an initial ceiling; the one-point
per-band dispatch cost is an uncalibrated estimate. These are implementation
choices, not measured account entitlements.

OpenCode's `minimax-coding-plan/MiniMax-M3` namespace supplies the existing
coding runtime. Reuse scoped vault/environment/native credential resolution,
account fingerprinting and launcher guards. Never fall back to the native
`minimax` pay-as-you-go credential. No managed MiniMax adapter is claimed.

Separately install official `mcode` (coding agent) and `mmx` (platform CLI).
MCode owns its browser login. Do not extract its OAuth tokens to make an API
key or imply that its login authenticates OpenCode. Orca can run MCode through
its supported terminal command interface; no built-in agent picker integration
or managed account binding is implied.

Sources checked 2026-09-21:
- https://platform.minimax.io/subscribe/token-plan
- https://github.com/MiniMax-AI/cli/blob/main/src/types/api.ts
- https://github.com/MiniMax-AI/cli/blob/main/src/utils/quota.ts
- https://models.dev/api.json (`minimax-coding-plan`)
- https://github.com/MiniMax-AI/minimax-code
