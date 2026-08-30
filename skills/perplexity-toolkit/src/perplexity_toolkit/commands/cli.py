"""CLI entry point for perplexity-toolkit."""

import argparse
import logging
import json
import sys

from ..config import set_config

logger = logging.getLogger(__name__)


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

    # Show verification results if available
    quality = result.get("quality")
    if quality:
        print(f"\n--- Quality Check ---")
        ac = quality.get("answer_check", {})
        sc = quality.get("source_check", {})
        print(f"Answer score: {ac.get('score', '?')}/100")
        if ac.get("issues"):
            for issue in ac["issues"]:
                print(f"  ⚠ {issue}")
        if sc.get("total", 0) > 0:
            print(f"Sources: {sc['valid']}/{sc['total']} reachable")
            for b in sc.get("broken_urls", []):
                print(f"  ✗ [{b['status']}] {b['href']}")
        print(f"Verdict: {quality.get('verdict', '?')} — {quality.get('suggestion', '')}")


def cmd_batch(args):
    """Batch search."""
    from ..batch import load_queries, run_batch
    queries = []
    if args.input:
        queries = load_queries(args.input)
    for q in (args.queries or []):
        queries.append({"query": q, "mode": args.mode})
    if not queries:
        logger.warning("No queries provided.")
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


def cmd_history(args):
    """Manage search history."""
    from ..history import list_conversations, find_conversation, delete_conversations
    if args.action == "list":
        from ..config import get_config
        from ..drivers import create_driver
        drv = create_driver(get_config())
        from ..config import get_config as gc
        drv.navigate(gc().base_url, new_tab=True)
        import time; time.sleep(gc().page_load_wait)
        convos = list_conversations(drv, args.limit)
        for i, c in enumerate(convos, 1):
            print(f"{i}. {c['title'][:60]}  [{c['href'][:20]}...]")
        print(f"\nTotal: {len(convos)} conversations")

    elif args.action == "search":
        from ..config import get_config
        from ..drivers import create_driver
        drv = create_driver(get_config())
        drv.navigate(get_config().base_url, new_tab=True)
        import time; time.sleep(get_config().page_load_wait)
        if not args.query:
            logger.warning("Error: provide a search query")
            sys.exit(1)
        convos = find_conversation(drv, args.query)
        for i, c in enumerate(convos, 1):
            print(f"{i}. {c['title'][:60]}  [{c['href'][:20]}...]")
        print(f"\nFound: {len(convos)} matching '{args.query}'")

    elif args.action == "delete":
        if not args.query and not args.href:
            logger.warning("Error: provide --query or --href")
            sys.exit(1)
        result = delete_conversations(query=args.query, hrefs=args.href,
                                       limit=args.limit, dry_run=args.dry_run)
        print(json.dumps(result, ensure_ascii=False, indent=2))


def build_parser() -> argparse.ArgumentParser:
    """Construct the argument parser with all subcommands."""
    parser = argparse.ArgumentParser(
        prog="perplexity",
        description="Perplexity Toolkit — automate Perplexity AI search",
    )
    parser.add_argument("-w", "--wait", type=float, help="Search wait time (seconds)")
    parser.add_argument("-r", "--retries", type=int, help="Max retries")
    parser.add_argument("-b", "--backend", help="Driver backend (webbridge, playwright)")
    parser.add_argument("--verify", action="store_true", help="Verify sources and answer quality")
    parser.add_argument("-v", "--verbose", action="store_true", help="Enable DEBUG logging")
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

    # history
    p = sub.add_parser("history", help="Manage search history")
    p.add_argument("action", choices=["list", "search", "delete"],
                   help="list: show all, search: find by title, delete: remove")
    p.add_argument("query", nargs="?", help="Search query or title to match")
    p.add_argument("--limit", type=int, default=50, help="Max conversations")
    p.add_argument("--dry-run", action="store_true", help="Show what would be deleted")
    p.add_argument("--href", nargs="*", help="Specific conversation UUIDs to delete")

    return parser


def main():
    parser = build_parser()
    args = parser.parse_args()
    # Apply global config overrides
    cfg_overrides = {}
    if args.wait is not None:
        cfg_overrides["search_wait"] = args.wait
    if args.retries is not None:
        cfg_overrides["max_retries"] = args.retries
    if args.backend:
        cfg_overrides["driver_backend"] = args.backend
    if cfg_overrides:
        set_config(**cfg_overrides)

    # Setup logging
    level = logging.DEBUG if args.verbose else logging.INFO
    logging.basicConfig(level=level, format="%(name)s %(levelname)s: %(message)s")

    if args.command == "search":
        cmd_search(args)
    elif args.command == "batch":
        cmd_batch(args)
    elif args.command == "aggregate":
        cmd_aggregate(args)
    elif args.command == "history":
        cmd_history(args)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
