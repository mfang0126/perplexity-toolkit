"""CLI entry point for perplexity-toolkit."""

import argparse
import json
import sys


def cmd_search(args):
    """Single search."""
    from ..search import search, deep_research, model_council, step_by_step
    modes = {"search": search, "deep_research": deep_research,
             "model_council": model_council, "step_by_step": step_by_step}
    fn = modes.get(args.mode, search)
    result = fn(args.query)
    if args.format == "json":
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print(f"URL: {result.get('url', 'N/A')}")
        print(f"Answer:\n{result.get('answer', 'No answer')}")
        print(f"\nSources ({len(result.get('sources', []))}):")
        for i, s in enumerate(result.get("sources", []), 1):
            print(f"  {i}. {s.get('text', '')[:80]} → {s.get('href', '')}")


def cmd_batch(args):
    """Batch search."""
    from ..batch import load_queries, run_batch
    queries = []
    if args.input:
        queries = load_queries(args.input)
    for q in (args.queries or []):
        queries.append({"query": q, "mode": args.mode})
    if not queries:
        print("No queries provided.", file=sys.stderr)
        sys.exit(1)
    run_batch(queries, output_file=args.output, resume=args.resume,
              progress_file=args.progress if args.resume else None,
              delay=args.delay)


def cmd_aggregate(args):
    """Aggregate results."""
    from ..aggregator import aggregate, to_markdown
    import json
    results = []
    for f in args.files:
        with open(f) as fh:
            data = json.load(fh)
            results.extend(data if isinstance(data, list) else [data])
    report = aggregate(results)
    if args.format == "markdown":
        print(to_markdown(report))
    else:
        print(json.dumps(report, ensure_ascii=False, indent=2))


def main():
    parser = argparse.ArgumentParser(
        prog="perplexity",
        description="Perplexity Toolkit — automate Perplexity AI search",
    )
    sub = parser.add_subparsers(dest="command")

    # search
    p = sub.add_parser("search", help="Single search")
    p.add_argument("query", help="Search query")
    p.add_argument("-m", "--mode", default="search",
                   choices=["search", "deep_research", "model_council", "step_by_step"])
    p.add_argument("-f", "--format", default="text", choices=["text", "json"])

    # batch
    p = sub.add_parser("batch", help="Batch search")
    p.add_argument("queries", nargs="*", help="Inline queries")
    p.add_argument("-i", "--input", help="Input file (json/csv/txt)")
    p.add_argument("-o", "--output", default="batch_results.json")
    p.add_argument("-m", "--mode", default="search")
    p.add_argument("-r", "--resume", action="store_true")
    p.add_argument("--progress", default=".batch_progress")
    p.add_argument("-d", "--delay", type=float, default=3.0)

    # aggregate
    p = sub.add_parser("aggregate", help="Aggregate results")
    p.add_argument("files", nargs="+", help="Result JSON files")
    p.add_argument("-f", "--format", default="json", choices=["json", "markdown"])

    args = parser.parse_args()
    if args.command == "search":
        cmd_search(args)
    elif args.command == "batch":
        cmd_batch(args)
    elif args.command == "aggregate":
        cmd_aggregate(args)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
