# Security

## Reporting

Report a vulnerability through GitHub's private advisory form:
<https://github.com/karoliang/rightsize/security/advisories/new>. Please do not
open a public issue for anything that exposes a credential.

Expect an acknowledgement within a week. This is a small project maintained in
spare time; there is no bounty.

## What this tool touches

rightsize reads more than it writes, but what it reads is sensitive, so it is
worth being explicit.

**It reads:**

- API keys, from the environment, from Infisical when the repo is linked, and
  from `~/.local/share/opencode/auth.json` (a file the OpenCode CLI already
  maintains).
- `~/.codex/sessions/**/rollout-*.jsonl`, for the `rate_limits` block only.
- `~/.claude/projects/**/*.jsonl`, for `usage` token counts only, and only when
  a Claude token budget is configured or `rightsize probe` is run.
- Your task text, which is sent to the TypeSafe API to be judged.

**It writes:**

- `~/.local/state/rightsize/state.json`: quota snapshots, the cached probe
  reading, exhausted marks, a free-request counter. No credentials.
- `registry.json` and `daily.log` in the repo, both gitignored.

**It never:**

- writes a credential into the repository, or into its state file;
- sends your code anywhere. Only the task description reaches TypeSafe, and
  nothing at all leaves the machine without a key present for that provider;
- proxies model traffic. It decides and exits, so it is not in the token path.

Your task descriptions are the thing to think about. If a spec would embarrass
you in a vendor's logs, do not route it: run `rightsize route` with a redacted
summary, or unset `TYPESAFE_API_KEY` and take the heuristic.

## Release check

Before publishing anything from this repository:

```bash
infisical scan                 # working tree
infisical scan --no-git=false  # full history
git ls-files | xargs grep -nE '(sk-|Bearer [A-Za-z0-9]{20,})'
```

Nine files are tracked. `registry.json`, `daily.log` and `.infisical.json` are
ignored deliberately: the first two churn, the third carries a project id.
