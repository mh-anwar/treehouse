# Shared skills

A catalog of agent skills on this box. Nothing here is active by default:
an agent may **suggest** one, but must ask its owner before installing it.

    box skills              # list the catalog and what you have installed
    box skills show NAME    # print a skill so you (and your owner) can read it first
    box skills add NAME     # install for Claude Code, Codex and opencode (symlinks)
    box skills rm NAME      # uninstall (only removes the symlinks box made)
    box skills share DIR    # (box admins) publish your own skill to everyone's catalog

| Skill | Say | What it does | Notes |
|---|---|---|---|
| `council` | "council" | Review from Claude Opus + GPT-6 Luna + Muse Spark Open + DeepSeek Flash, looping until they agree | Needs OpenCode with an OpenRouter key; ~15-20¢/round |
| `full-send` | "full send" | Proves, files, babysits and **merges** a PR with nobody watching | Uses `sol-loop`, `file-pr`, `babysit-pr`. Merges on its own |
| `full-stop` | "full stop" | Same as `full-send`, stops when the PR is ready for you to merge | Uses `full-send` |
| `sol-loop` | "sol loop" | Reviews a change with GPT Sol in a loop until both agents agree | GPT Sol is pricey via OpenRouter; fine on a ChatGPT/Codex plan |
| `file-pr` | "file a PR" | Files a short PR that leads with the problem | Uses `leaving-pr-comments` |
| `takeover` | "take over" | Finishes someone else's PR, branch or agent thread, keeps their credit | Uses `modernize`, `file-pr`, `babysit-pr`, `leaving-pr-comments` |
| `babysit-pr` | "babysit" | Watches a PR through CI and review bots, fixes real findings | Needs `gh` logged in; ships `scripts/pr-wait` |
| `modernize` | "modernize" | Rebases an old PR and decides if it should still exist | Uses `leaving-pr-comments` |
| `leaving-pr-comments` | (automatic) | Formats comments an agent posts on your behalf | |
| `skill-creator` | "make a skill" | Writes, tests and improves skills (then `box skills share` it) | Runs `claude -p` for evals |
| `webapp-testing` | (automatic) | Drives a local web app with Playwright to test it | Pass `--no-sandbox` to Chromium on this box |
| `frontend-design` | (automatic) | Distinctive visual design for new or reworked UI | |
| `mcp-builder` | "build an MCP server" | Guide + eval harness for writing MCP servers | Evals need an Anthropic API key |

Install a skill's dependencies too (the Notes column lists them).

## Where they come from

Unchanged copies, refreshed with `./sync-upstream.sh` (exact commits in `UPSTREAM`):

- Theo Browne's [tstack](https://github.com/t3dotgg/tstack): the PR skills (MIT, `LICENSE-tstack`).
  `council` is our own adaptation of his, with cheap OpenRouter members.
- Anthropic's [skills](https://github.com/anthropics/skills): `skill-creator`, `webapp-testing`,
  `frontend-design`, `mcp-builder` (Apache-2.0, `LICENSE.txt` in each).

To refresh: run `./sync-upstream.sh`, review `git diff share/skills`, commit, then the owner re-runs
`install.sh`. The script never touches `council` or skills admins published with `box skills share`.
