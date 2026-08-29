#!/usr/bin/env python3
"""
Perplexity Batch Search Pipeline — search multiple queries with resume support.

Usage:
    # CLI with queries
    python batch_search.py "query 1" "query 2" "query 3"

    # From JSON file
    python batch_search.py --input queries.json --output results.json

    # From CSV file (one query per line)
    python batch_search.py --input queries.csv --output results.json

    # Resume interrupted batch
    python batch_search.py --input queries.json --output results.json --resume

Input JSON format: ["query 1", "query 2", ...] or [{"query": "...", "mode": "deep_research"}, ...]
Input CSV format: one query per line (first line is header if starts with "query")
"""

import json
import csv
import sys
import os
import time
import argparse
from typing import Optional
from pathlib import Path
from datetime import datetime

# Add parent dir to path for import
sys.path.insert(0, str(Path(__file__).parent))
from perplexity_search import (
    perplexity_search,
    perplexity_deep_research,
    perplexity_model_council,
    perplexity_step_by_step,
)

MODE_FUNCTIONS = {
    "search": perplexity_search,
    "deep_research": perplexity_deep_research,
    "model_council": perplexity_model_council,
    "step_by_step": perplexity_step_by_step,
}

DEFAULT_MODE = "search"


def load_queries(source: str) -> list[dict]:
    """Load queries from file or CLI args. Returns list of {query, mode} dicts."""
    path = Path(source)
    if not path.exists():
        # Treat as inline query
        return [{"query": source, "mode": DEFAULT_MODE}]

    suffix = path.suffix.lower()
    if suffix == ".json":
        with open(path) as f:
            data = json.load(f)
        if isinstance(data, list):
            queries = []
            for item in data:
                if isinstance(item, str):
                    queries.append({"query": item, "mode": DEFAULT_MODE})
                elif isinstance(item, dict):
                    queries.append({
                        "query": item.get("query", ""),
                        "mode": item.get("mode", DEFAULT_MODE),
                    })
            return queries
    elif suffix == ".csv":
        queries = []
        with open(path, newline="") as f:
            reader = csv.DictReader(f)
            for row in reader:
                q = row.get("query", row.get("q", ""))
                m = row.get("mode", DEFAULT_MODE)
                if q:
                    queries.append({"query": q, "mode": m})
        return queries
    elif suffix == ".txt":
        with open(path) as f:
            return [{"query": line.strip(), "mode": DEFAULT_MODE}
                    for line in f if line.strip()]

    return [{"query": source, "mode": DEFAULT_MODE}]


def load_progress(progress_file: str) -> set[str]:
    """Load set of already-searched queries from progress file."""
    if not os.path.exists(progress_file):
        return set()
    with open(progress_file) as f:
        return {line.strip() for line in f if line.strip()}


def save_progress(progress_file: str, query: str):
    """Append a completed query to the progress file."""
    with open(progress_file, "a") as f:
        f.write(query + "\n")


def run_batch(
    queries: list[dict],
    output_file: str,
    progress_file: Optional[str] = None,
    delay: float = 3.0,
    wait_seconds: int = 15,
    resume: bool = False,
) -> list[dict]:
    """Run batch search with optional resume and rate limiting."""
    done_set = set()
    if resume and progress_file:
        done_set = load_progress(progress_file)

    results = []
    total = len(queries)
    skipped = 0

    for i, item in enumerate(queries):
        query = item["query"]
        mode = item.get("mode", DEFAULT_MODE)

        if query in done_set:
            skipped += 1
            print(f"[{i+1}/{total}] SKIP (done): {query[:60]}", file=sys.stderr)
            continue

        print(f"[{i+1}/{total}] {mode}: {query[:60]}...", file=sys.stderr)

        fn = MODE_FUNCTIONS.get(mode, perplexity_search)
        try:
            if mode == "deep_research":
                result = fn(query, wait_seconds=max(wait_seconds, 90))
            elif mode == "model_council":
                result = fn(query, wait_seconds=max(wait_seconds, 25))
            elif mode == "step_by_step":
                result = fn(query, wait_seconds=max(wait_seconds, 20))
            else:
                result = fn(query, wait_seconds=wait_seconds)
        except Exception as e:
            result = {"error": str(e), "query": query, "mode": mode}

        result["query"] = query
        result["searched_at"] = datetime.now().isoformat()
        results.append(result)

        # Save progress
        if progress_file:
            save_progress(progress_file, query)

        # Rate limit
        if i < total - 1:
            time.sleep(delay)

    # Write output
    with open(output_file, "w") as f:
        json.dump(results, f, ensure_ascii=False, indent=2)

    print(f"\nDone: {len(results)} searched, {skipped} skipped, "
          f"output: {output_file}", file=sys.stderr)
    return results


def main():
    parser = argparse.ArgumentParser(description="Perplexity Batch Search")
    parser.add_argument("queries", nargs="*", help="Inline queries")
    parser.add_argument("-i", "--input", help="Input file (json/csv/txt)")
    parser.add_argument("-o", "--output", default="batch_results.json",
                        help="Output JSON file")
    parser.add_argument("-r", "--resume", action="store_true",
                        help="Resume from progress file")
    parser.add_argument("-p", "--progress", default=".batch_progress",
                        help="Progress file path")
    parser.add_argument("-d", "--delay", type=float, default=3.0,
                        help="Delay between searches (seconds)")
    parser.add_argument("-w", "--wait", type=int, default=15,
                        help="Wait seconds per search")
    parser.add_argument("-m", "--mode", default=DEFAULT_MODE,
                        choices=list(MODE_FUNCTIONS.keys()),
                        help="Default search mode")
    args = parser.parse_args()

    # Collect queries
    all_queries = []
    if args.input:
        all_queries = load_queries(args.input)
    for q in args.queries:
        all_queries.append({"query": q, "mode": args.mode})

    if not all_queries:
        parser.print_help()
        sys.exit(1)

    run_batch(
        queries=all_queries,
        output_file=args.output,
        progress_file=args.progress if args.resume else None,
        delay=args.delay,
        wait_seconds=args.wait,
        resume=args.resume,
    )


if __name__ == "__main__":
    main()
