---
name: council
description: Trigger this if the user says 'council'.
---

# Council

Adapted from Theo Browne's `council` skill (github.com/t3dotgg/tstack,
commit 1085050, MIT). Same process; the members are fixed and cheap, because
everything except Claude is paid per token through OpenRouter. Each account uses
its own OpenRouter key (`opencode auth login`); a round costs about 2-3 cents.

Before an audit, address outstanding PR comments, fix known issues, and run
relevant checks. Give council the updated implementation; do not defer known
fixes until reviews return. If the user wants to discard the implementation
and explore new ideas, skip fixes to code being replaced.

Bring in four delegates to help with the user's request, usually for review
or feedback. One model from each of four providers. If the user's instructions
name council members, use those. Otherwise use exactly these:

| Delegate | Provider | Model |
| --- | --- | --- |
| Claude Opus | Claude (native, needs a Claude login) | `opus`, high effort |
| GPT-6 Luna | Codex (ChatGPT login); else OpenCode → OpenRouter | `gpt-6-luna` on Codex; else `openrouter/openai/gpt-6-luna` |
| Muse Spark Contributor | OpenCode → OpenRouter | `openrouter/meta/muse-spark-1.3-contributor` |
| DeepSeek Flash | OpenCode → DeepSeek platform; else OpenRouter | `deepseek/deepseek-flash`; else `openrouter/~deepseek/deepseek-flash-latest` |

Never swap in a pricier model (GPT Terra/Sol, GPT-5.6 Luna, Muse Spark/Wonder, DeepSeek Pro, Gemini), and
never run Claude through OpenRouter.

In T3 Code, use `orchestrator_capabilities` to resolve the models, then
`delegate_task` to start one delegate per model in parallel: Claude on the
`claudeAgent` provider (or the native `Agent` tool with `model: "opus"` when
you are Claude), GPT-6 Luna on the `codex` provider (free on a ChatGPT plan;
use the OpenRouter id only if Codex is unavailable or rate-limited), the others with `target: {providerInstanceId: "opencode",
model: "<id>"}`. Elsewhere, use the harness's subagent tools, or run
`opencode run -m <id> "<brief>"` for each OpenRouter member in parallel. If
OpenCode is disabled or a named member is unavailable, report it instead of
substituting.

Give each delegate the request and relevant context, the same brief for all,
without your own conclusions. For review or feedback, keep their work
read-only. Collect their responses and summarize the useful findings,
disagreements, and recommended next steps. Check each finding against the code
before acting on it; the small models can be confidently wrong.

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
| GPT-6 Luna | 8/10 | 10/10 |
| Muse Spark Contributor | 7/10 | 9/10 |
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
