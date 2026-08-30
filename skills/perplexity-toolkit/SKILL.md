---
name: perplexity-toolkit
description: "Automate Perplexity AI search — batch, extract, aggregate, verify."
version: 1.0.0
author: mfang0126
license: MIT
metadata:
  hermes:
    tags: [search, perplexity, research, batch, automation, browser]
  platforms: [macos, linux]
---

# perplexity-toolkit

Automate Perplexity AI search via browser control — search, extract, batch, and analyze.

## When to Use

- Batch search hundreds of queries with resume support
- Extract all cited sources from Perplexity results
- Aggregate and deduplicate sources across multiple searches
- Research tasks that need Perplexity's Deep Research or Model Council modes

## Quick Start

```bash
git clone https://github.com/mfang0126/perplexity-toolkit.git
cd perplexity-toolkit
pip install -e .
```

## CLI

```bash
perplexity search "query"                    # Standard search
perplexity search "query" --mode deep        # Deep research
perplexity search "query" --mode council     # Model council
perplexity batch queries.txt                 # Batch from file
perplexity aggregate results/ --output report.md
perplexity history                           # View search history
```

## 4 Search Modes

| Mode | What It Does |
|------|-------------|
| Standard | Quick search with citations |
| Deep Research | Multi-step, thorough investigation |
| Model Council | Multiple models debate the answer |
| Step-by-step | Learning-oriented breakdown |

## Requirements

- Python 3.10+
- Playwright (browser automation)
- Perplexity account (free tier works)
