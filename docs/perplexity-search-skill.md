---
name: perplexity-search
description: "Perplexity search with anti-hallucination, model selection, and source verification."
---

# Perplexity Intelligent Search Skill

Intent-driven: picks the right model, crafts anti-hallucination prompts, verifies sources, auto-retries on failure.

## When to Use

- User says "perplexity search", "用 perplexity 查"
- Price comparison, technical evaluation, market research with verified citations

## Step 1: Model Selection

| Task | Model |
|------|-------|
| Price comparison | GPT-4o + Pro Search |
| Technical eval | Claude 3.5 Sonnet |
| Academic | GPT-4o + Academic Focus |
| Deep report | Deep Research |
| Multi-perspective | Model Council |
| Quick lookup | Sonar (default) |

## Step 2: Anti-Hallucination Prompts

NEVER ask a bare question. Wrap with:

```
{query}
Requirements:
1. Cite sources with URLs
2. If no verified source, say so
3. For pricing: official page URLs only
4. Format as comparison table with Source URL column
```

## Step 3: Verification

- Check all cited URLs (HTTP HEAD)
- Score answer quality 0-100
- Red flags: prices without URLs, "studies show" without link

## Step 4: Auto-Retry

1. Add verification prompt
2. Switch model
3. Break into sub-queries
4. Intent-driven: screenshot, redo flow, save new script

## Step 5: Script Versioning

Save successful patterns as versioned YAML scripts under `scripts/perplexity/`.
