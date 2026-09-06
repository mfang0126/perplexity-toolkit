# Agent skills

The CLI in this repo is only half of the story. These three skills carry the
operational knowledge an agent needs to drive Perplexity well: which route to
take, how to keep browser sessions from fragmenting, and when an answer is
allowed to be called verified.

They are plain Markdown with YAML frontmatter, so any agent framework that
loads `SKILL.md` files can use them.

## What each one is for

| Skill | Layer | Use it for |
|---|---|---|
| `perplexity-search` | Policy | Quick lookups. **Owns the canonical definition** of the candidate-vs-verified boundary and the HEAD-vs-readback rule |
| `perplexity-conversational-research` | Workflow | Deep or multi-turn research. Picks between the CLI and the browser route, and keeps one session across follow-ups |
| `perplexity-web-automation` | Execution | The low-level logged-in Chrome / WebBridge actions, plus a standalone `scripts/perplexity_search.py` helper |

They are designed to be loaded together. `perplexity-search` is the single
source of truth for the verification rule; the other two link to it rather
than restating it, so the rule can be changed in one place.

## Install

Point your agent's skill directory at these, rather than copying them — a copy
goes stale the moment this repo updates.

```bash
git clone https://github.com/mfang0126/perplexity-toolkit.git ~/src/perplexity-toolkit

# Symlink into wherever your framework loads skills from.
# Claude Code / Hermes example:
ln -s ~/src/perplexity-toolkit/skills/perplexity-search           ~/.claude/skills/perplexity-search
ln -s ~/src/perplexity-toolkit/skills/perplexity-web-automation   ~/.claude/skills/perplexity-web-automation
ln -s ~/src/perplexity-toolkit/skills/perplexity-conversational-research \
                                                                  ~/.claude/skills/perplexity-conversational-research
```

Then `git pull` in the clone to update all three at once.

## Two reliability tiers, on purpose

`perplexity-web-automation/scripts/perplexity_search.py` reimplements logic that
also exists in `perplexity_toolkit.search`. That overlap is deliberate.

| | `perplexity_toolkit` | `perplexity_search.py` |
|---|---|---|
| Imports | its own package (config, drivers, utils, i18n, verify) | standard library only, shells out to `curl` |
| Runs when nothing is installed | no | yes |
| Role | primary executor | fallback and diagnostic |
| Retry / backoff | yes (`_search_with_retry`) | none, by design |

The script's independence is its entire value. A toolkit installed into one
environment is invisible to an agent running a different interpreter — an
observed failure mode, not a hypothetical one. If the script imported the
toolkit, the fallback would vanish precisely when it is needed, and nobody
would notice until the day the toolkit broke.

So: **do not deduplicate these two.** If shared logic genuinely needs to move,
vendor it as a stdlib-only module inside `scripts/`. The missing retry is also
intentional — a diagnostic should show the raw failure rather than mask it.

## Requirements

The skills reference the `perplexity` CLI as an optional route and Kimi
WebBridge as the browser layer. See the repo README for installing the CLI
(`pipx` is recommended so it lands on `PATH`) and for the setup self-check.

Cross-references between these skills use skill **names**, never absolute
paths, so they resolve regardless of where you install them.
