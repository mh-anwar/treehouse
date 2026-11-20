---
name: council
description: Trigger this if the user says 'council'.
---

# Council

Adapted from Theo Browne's `council` skill (github.com/t3dotgg/tstack,
commit 11e37a7, MIT). Same process, with two lineups: a cheaper one of ours
and Theo's original flagship one.

## Setup (first run, or when `~/.config/council.md` is missing)

Do this right after installing the skill, before the first council, and when
the user asks to change members.
Keep it to one short message and one question.

1. Check what the user already pays for. Use `orchestrator_capabilities` in
   T3 Code; elsewhere check the logins (`claude`, `codex login status`, a
   Google login for Antigravity or the Gemini CLI). A subscription makes that
   member cost nothing extra:
   - Claude login: Claude runs natively.
   - ChatGPT login: GPT runs on Codex.
   - Google AI login: Gemini runs on Antigravity or the Gemini CLI.
   Every member with no subscription goes through OpenRouter.
2. Tell the user they need an **OpenRouter key** (openrouter.ai/keys, prepaid
   credit) for every member not covered by a subscription. Without it those
   members fail. Say which ones that is for them.
3. Offer the two lineups with the rough cost per review round for their
   setup, and suggest **Standard** unless they want the strongest review:

   | | Standard (ours, cheaper) | Full (Theo's original) |
   | --- | --- | --- |
   | Members | Claude Opus, GPT Terra, Gemini Neon, DeepSeek Flash | Claude Opus, GPT Sol, Gemini Neon |
   | Effort | high | high |
   | Strength | four opinions, one nearly free | strongest GPT |

   Estimate the cost from live prices (`curl -s
   https://openrouter.ai/api/v1/models`): a member's round is roughly 100k
   tokens in and 10k out. In late 2026 that was about 45¢ for Gemini Neon,
   60¢ for Opus, 30¢ for Sol, 15¢ for Terra and 1¢ for DeepSeek Flash, so
   about $1.20-1.35 a round with everything on OpenRouter, or about 45¢ with
   Claude and ChatGPT subscriptions. They can also swap or drop any member.
4. If they need a key and do not have one in OpenCode yet
   (`~/.local/share/opencode/auth.json` has no `openrouter` entry), get it
   without chat: in T3 Code use `request_secret` with `delivery: "file"`, then
   write `{"openrouter": {"type": "api", "key": "<key>"}}` into that file
   (merge, keep other entries, mode 600). Never ask for it in chat or put it
   in a command line. Elsewhere, ask them to run `opencode auth login`.
5. Save the choice to `~/.config/council.md`: the lineup name, each member
   with its provider and model id, and the date. Read it on every later run
   and use it instead of the defaults.

## Members

Resolve "latest" to the newest model of that family that the catalog has
today (`orchestrator_capabilities`, or `opencode models`). If a member is
unavailable, report it instead of substituting a pricier model.

| Member | With a subscription | Otherwise (OpenCode → OpenRouter) |
| --- | --- | --- |
| Claude Opus | native (`Agent` with `model: "opus"`, or `claudeAgent`) | `openrouter/~anthropic/claude-opus-latest` |
| GPT Terra (Standard) | Codex, newest Terra (`gpt-6.1-terra` today) | `openrouter/openai/gpt-6.1-terra`, or newer |
| GPT Sol (Full) | Codex, newest Sol (`gpt-6.2-sol` today) | `openrouter/openai/gpt-6.2-sol`, or newer |
| Gemini Neon | Antigravity / Gemini CLI | `openrouter/google/gemini-4-neon`, or newer flagship |
| DeepSeek Flash (Standard) | DeepSeek platform key in OpenCode | `openrouter/~deepseek/deepseek-flash-latest` |

If the user's instructions name council members, use those.

## Process

Before an audit, address outstanding PR comments, fix known issues, and run
relevant checks. Give council the updated implementation; do not defer known
fixes until reviews return. If the user wants to discard the implementation
and explore new ideas, skip fixes to code being replaced.

Bring in one delegate per member of the saved lineup, usually for review or
feedback. In T3 Code, use `delegate_task` to start them in parallel, one per
model (`target: {providerInstanceId: "opencode", model: "<id>"}` for the
OpenRouter ones). Elsewhere, use the harness's subagent tools, or run
`opencode run -m <id> "<brief>"` for each OpenRouter member in parallel.

Give each delegate the request and relevant context, the same brief for all,
without your own conclusions. For review or feedback, keep their work
read-only. Collect their responses and summarize the useful findings,
disagreements, and recommended next steps. Check each finding against the code
before acting on it; the smaller models can be confidently wrong.

Do not wait idle. When a result arrives, check its findings against the code
at once and decide what to fix while the other delegates run. Do not edit
files while a delegate still reviews the same checkout. If the user's
instructions mark a delegate as non-blocking, do not wait for it; treat a
late result as feedback and do not record its score.

For a code review, ask each delegate for a score out of 10.
Count each full review pass as one round.
A fresh review after fixes starts a new round.

If the work ends in a pull request, put the results in the PR description.
Put this section before the model and harness line at the end:

```markdown
## Council review

Rounds: 2

| Delegate | Round 1 | Round 2 |
| --- | --- | --- |
| Claude Opus | 7/10 | 9/10 |
| GPT Terra | 8/10 | 10/10 |
| Gemini Neon | 7/10 | 9/10 |
| DeepSeek Flash | 7/10 | 9/10 |
```

If a delegate gives no score, write `no score`. Do not invent one.
If the PR is open, edit the description. Do not post a comment for this.
If there is no PR, put the same results in the final reply only.

If you agree with their recommendations, make the changes and request a fresh
review after. Continue this process until all delegates are satisfied with the
outcome.

Do not let repeated reviews result in scope creep. It is your job to fight to
keep scope limited.
