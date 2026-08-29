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
    """Run batch search with optional resume and rate limiting.

    Progress is always tracked in progress_file (if given): each query|||mode
    is appended only AFTER a successful search, so any batch — not just ones
    started with resume=True — can be resumed later. Failed searches are not
    marked done and are retried on resume. Output JSON is rewritten
    incrementally after every item so a mid-batch crash loses no completed work.
    """
    cfg = config or get_config()
    batch_delay = delay or cfg.batch_delay

    done_set = set()
    if progress_file and os.path.exists(progress_file):
        with open(progress_file) as f:
            done_set = {line.strip() for line in f if line.strip()}

    results = []
    total = len(queries)
    skipped = 0
    invalid = 0
    failed = 0

    def write_output():
        with open(output_file, "w") as f:
            json.dump(results, f, ensure_ascii=False, indent=2)

    for i, item in enumerate(queries):
        query = item.get("query", "")
        mode = item.get("mode", "search")

        if not query:
            invalid += 1
            print(f"[{i+1}/{total}] WARNING: empty query, skipping", file=sys.stderr)
            continue

        if mode not in MODE_FUNCTIONS:
            invalid += 1
            print(
                f"[{i+1}/{total}] WARNING: unknown mode '{mode}' for query "
                f"'{query[:60]}', skipping",
                file=sys.stderr,
            )
            continue

        key = f"{query}|||{mode}"
        if resume and key in done_set:
            skipped += 1
            continue

        print(f"[{i+1}/{total}] {mode}: {query[:60]}...", file=sys.stderr)

        ok = False
        try:
            result = MODE_FUNCTIONS[mode](query, config=cfg)
            ok = True
        except Exception as e:
            failed += 1
            result = {"error": str(e), "query": query, "mode": mode}
            print(f"  ERROR [{mode} {query[:60]}]: {e}", file=sys.stderr)

        result["query"] = query
        result["searched_at"] = datetime.now().isoformat()
        results.append(result)
        write_output()

        if ok and progress_file:
            with open(progress_file, "a") as f:
                f.write(key + "\n")

        if i < total - 1:
            time.sleep(batch_delay)

    # Ensure the output file exists even if every item was skipped/invalid.
    write_output()

    print(
        f"\nDone: {len(results)} searched, {skipped} resumed-skipped, "
        f"{invalid} invalid/empty skipped, {failed} failed → {output_file}",
        file=sys.stderr,
    )
    return results
