"""Batch search pipeline with resume and rate limiting."""

import json
import csv
import sys
import os
import time
from typing import Optional, List
from pathlib import Path
from datetime import datetime

from .config import Config, get_config
from .search import search, deep_research, model_council, step_by_step, SearchResult

MODE_FUNCTIONS = {
    "search": search,
    "deep_research": deep_research,
    "model_council": model_council,
    "step_by_step": step_by_step,
}


def load_queries(source: str) -> List[dict]:
    """Load queries from file or inline string. Returns [{query, mode}]."""
    path = Path(source)
    if not path.exists():
        return [{"query": source, "mode": "search"}]

    suffix = path.suffix.lower()
    queries = []

    if suffix == ".json":
        with open(path) as f:
            data = json.load(f)
        for item in (data if isinstance(data, list) else [data]):
            if isinstance(item, str):
                queries.append({"query": item, "mode": "search"})
            elif isinstance(item, dict):
                queries.append({"query": item.get("query", ""), "mode": item.get("mode", "search")})
    elif suffix == ".csv":
        with open(path, newline="") as f:
            for row in csv.DictReader(f):
                q = row.get("query", row.get("q", ""))
                if q:
                    queries.append({"query": q, "mode": row.get("mode", "search")})
    elif suffix == ".txt":
        with open(path) as f:
            queries = [{"query": l.strip(), "mode": "search"} for l in f if l.strip()]

    return queries or [{"query": source, "mode": "search"}]


def run_batch(
    queries: List[dict],
    output_file: str = "batch_results.json",
    config: Optional[Config] = None,
    progress_file: Optional[str] = None,
    resume: bool = False,
    delay: Optional[float] = None,
) -> List[SearchResult]:
    """Run batch search with optional resume and rate limiting."""
    cfg = config or get_config()
    batch_delay = delay or cfg.batch_delay

    done_set = set()
    if resume and progress_file and os.path.exists(progress_file):
        with open(progress_file) as f:
            done_set = {line.strip() for line in f if line.strip()}

    results = []
    total = len(queries)
    skipped = 0

    for i, item in enumerate(queries):
        query = item["query"]
        mode = item.get("mode", "search")

        if query in done_set:
            skipped += 1
            continue

        print(f"[{i+1}/{total}] {mode}: {query[:60]}...", file=sys.stderr)
        fn = MODE_FUNCTIONS.get(mode, search)

        try:
            result = fn(query, config=cfg)
        except Exception as e:
            result = {"error": str(e), "query": query, "mode": mode}

        result["query"] = query
        result["searched_at"] = datetime.now().isoformat()
        results.append(result)

        if progress_file:
            with open(progress_file, "a") as f:
                f.write(query + "\n")

        if i < total - 1:
            time.sleep(batch_delay)

    with open(output_file, "w") as f:
        json.dump(results, f, ensure_ascii=False, indent=2)

    print(f"\nDone: {len(results)} searched, {skipped} skipped → {output_file}", file=sys.stderr)
    return results
