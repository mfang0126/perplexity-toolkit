#!/usr/bin/env python3
"""
Perplexity Result Aggregator — merge multiple search results into a consolidated report.

Usage:
    # Aggregate from a single batch result file
    python result_aggregator.py batch_results.json

    # Aggregate from multiple files
    python result_aggregator.py results1.json results2.json --output report.json

    # Generate markdown report
    python result_aggregator.py batch_results.json --format markdown --output report.md
"""

import json
import sys
import argparse
from collections import Counter
from pathlib import Path
from urllib.parse import urlparse


def load_results(files: list[str]) -> list[dict]:
    """Load results from one or more JSON files."""
    all_results = []
    for f in files:
        with open(f) as fh:
            data = json.load(fh)
            if isinstance(data, list):
                all_results.extend(data)
            elif isinstance(data, dict):
                all_results.append(data)
    return all_results


def deduplicate_sources(results: list[dict]) -> list[dict]:
    """Deduplicate sources across all results, rank by citation frequency."""
    url_counts = Counter()
    url_info = {}

    for r in results:
        for src in r.get("sources", []):
            href = src.get("href", "")
            if not href:
                continue
            # Normalize URL
            parsed = urlparse(href)
            normalized = f"{parsed.scheme}://{parsed.netloc}{parsed.path}".rstrip("/")
            url_counts[normalized] += 1
            if normalized not in url_info:
                url_info[normalized] = {
                    "url": href,
                    "text": src.get("text", "")[:150],
                    "domain": parsed.netloc,
                    "cited_count": 0,
                }

    # Update counts
    for url, count in url_counts.items():
        url_info[url]["cited_count"] = count

    # Sort by citation frequency
    ranked = sorted(url_info.values(), key=lambda x: x["cited_count"], reverse=True)
    return ranked


def extract_key_facts(results: list[dict]) -> list[str]:
    """Extract key facts from answer texts (first sentence of each answer)."""
    facts = []
    for r in results:
        answer = r.get("answer", "")
        if not answer:
            continue
        # Get first meaningful paragraph
        lines = [l.strip() for l in answer.split("\n") if l.strip()]
        for line in lines:
            # Skip headers and metadata
            if line.startswith(("答案", "链接", "图片", "分享", "已完成")):
                continue
            if len(line) > 30:
                facts.append(line[:300])
                break
    return facts


def aggregate_results(results: list[dict]) -> dict:
    """Aggregate multiple search results into a consolidated report."""
    sources = deduplicate_sources(results)
    key_facts = extract_key_facts(results)

    # Collect all follow-ups
    all_follow_ups = []
    seen_follow_ups = set()
    for r in results:
        for f in r.get("follow_ups", []):
            if f not in seen_follow_ups:
                all_follow_ups.append(f)
                seen_follow_ups.add(f)

    # Stats
    modes_used = Counter(r.get("mode", "search") for r in results)
    errors = [r for r in results if r.get("error")]

    return {
        "total_searches": len(results),
        "successful": len(results) - len(errors),
        "errors": len(errors),
        "modes_used": dict(modes_used),
        "unique_sources": len(sources),
        "top_sources": sources[:20],
        "key_facts": key_facts[:10],
        "follow_up_questions": all_follow_ups[:10],
        "queries": [
            {"query": r.get("query", ""), "mode": r.get("mode", "search"),
             "url": r.get("url", ""), "answer_len": len(r.get("answer", "")),
             "sources_count": len(r.get("sources", []))}
            for r in results
        ],
    }


def to_markdown(report: dict) -> str:
    """Convert aggregated report to markdown."""
    lines = [
        "# Perplexity Search Report",
        f"\n**Total searches:** {report['total_searches']}",
        f"**Successful:** {report['successful']}",
        f"**Errors:** {report['errors']}",
        f"**Unique sources:** {report['unique_sources']}",
        f"\n**Modes used:** {', '.join(f'{k}({v})' for k,v in report['modes_used'].items())}",
        "\n---",
        "\n## Top Sources (by citation frequency)",
    ]

    for i, src in enumerate(report["top_sources"][:15], 1):
        lines.append(f"{i}. **[{src['domain']}]({src['url']})** "
                     f"({src['cited_count']}x cited) — {src['text'][:80]}...")

    lines.append("\n## Key Findings")
    for i, fact in enumerate(report["key_facts"], 1):
        lines.append(f"{i}. {fact}")

    lines.append("\n## Suggested Follow-ups")
    for q in report["follow_up_questions"]:
        lines.append(f"- {q}")

    lines.append("\n## Query Details")
    lines.append("| Query | Mode | Sources | Answer Len |")
    lines.append("|-------|------|---------|------------|")
    for q in report["queries"]:
        lines.append(f"| {q['query'][:50]} | {q['mode']} | {q['sources_count']} | {q['answer_len']} |")

    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(description="Aggregate Perplexity search results")
    parser.add_argument("files", nargs="+", help="Result JSON files")
    parser.add_argument("-o", "--output", help="Output file")
    parser.add_argument("-f", "--format", choices=["json", "markdown"], default="json")
    args = parser.parse_args()

    results = load_results(args.files)
    report = aggregate_results(results)

    if args.format == "markdown":
        output = to_markdown(report)
    else:
        output = json.dumps(report, ensure_ascii=False, indent=2)

    if args.output:
        with open(args.output, "w") as f:
            f.write(output)
        print(f"Report written to {args.output}", file=sys.stderr)
    else:
        print(output)


if __name__ == "__main__":
    main()
