"""CLI entry point for perplexity-toolkit."""

import argparse
import logging
import json
import sys

from ..config import set_config
from ..routing import select_route

logger = logging.getLogger(__name__)


def cmd_route(args) -> int:
    """Select a route from the original user wording only."""
    decision = select_route(args.request)
    if args.format == "json":
        print(json.dumps(decision, ensure_ascii=False))
    else:
        print(f"route: {decision['route']}")
        if decision["matched_terms"]:
            print("matched_terms: " + ", ".join(decision["matched_terms"]))
        print("reason: " + decision["reason"])
    return 0


def cmd_console(args) -> int:
    """Resident Perplexity console actions."""
    from ..console import (ConsoleError, console_ask, console_models,
                           console_selfcheck, console_set_model,
                           console_status, console_threads)
    action = getattr(args, "console_action", None)
    fmt = getattr(args, "format", "text")

    if action in ("models", "model"):
        try:
            payload = (console_models() if action == "models"
                       else console_set_model(args.name))
        except ConsoleError as exc:
            payload = {"ok": False, "gate": exc.gate, "error": exc.message,
                       "evidence": exc.evidence, "gates": exc.gates}
            if fmt == "json":
                print(json.dumps(payload, ensure_ascii=False, indent=2))
            else:
                print(f"FAILED at gate: {exc.gate}")
                print(f"  {exc.message}")
                if exc.evidence:
                    print(f"  evidence: {exc.evidence}")
            return 1
        if fmt == "json":
            print(json.dumps(payload, ensure_ascii=False, indent=2))
        else:
            if action == "models":
                print(f"current: {payload['current']}")
                for m in payload["models"]:
                    mark = "*" if m["checked"] else " "
                    badges = f"  [{'/'.join(m['badges'])}]" if m["badges"] else ""
                    sub = "  (submenu)" if m["submenu"] else ""
                    print(f"{mark} {m['name']}{badges}{sub}")
                print(f"menu_closed: {payload.get('menu_closed')}")
            else:
                print(f"switched: {payload['from']} -> {payload['to']} "
                      f"(attempts={payload['attempts']})")
        return 0

    if action == "ask":
        try:
            result = console_ask(args.query, task=args.task,
                                 new_thread=args.new_thread,
                                 wait_budget=args.wait_budget,
                                 files=getattr(args, "file", None))
        except ConsoleError as exc:
            payload = {"ok": False, "gate": exc.gate, "error": exc.message,
                       "evidence": exc.evidence, "gates": exc.gates}
            if fmt == "json":
                print(json.dumps(payload, ensure_ascii=False, indent=2))
            else:
                print(f"FAILED at gate: {exc.gate}")
                print(f"  {exc.message}")
                if exc.evidence:
                    print(f"  evidence: {exc.evidence}")
            return 1
        if fmt == "json":
            print(json.dumps(result, ensure_ascii=False, indent=2))
        else:
            print(result["answer"])
            print("\n--- console ---")
            print(f"task: {result['task']}  session: {result['session']}"
                  f"  new_thread: {result['new_thread']}  elapsed: {result['elapsed_s']}s")
            print(f"url: {result['url']}")
            print(f"model: {result.get('model') or '-'}  "
                  f"attachments: {', '.join(result.get('attachments') or []) or '-'}")
            print(f"sources: {len(result['sources'])}")
            gate_bits = []
            for name, value in result["gates"].items():
                gate_bits.append(f"{name}={'ok' if isinstance(value, dict) else value}")
            print("gates: " + ", ".join(gate_bits))
        return 0

    if action == "status":
        payload = console_status()
        if fmt == "json":
            print(json.dumps(payload, ensure_ascii=False, indent=2))
        else:
            print(f"session: {payload['session']}  group: {payload['group_title']}")
            print(f"active_task: {payload['active_task']}")
            for name, entry in payload["threads"].items():
                print(f"  - {name}: {entry.get('url', '')}  (turns={entry.get('turns', '?')})")
            print(f"live: {json.dumps(payload.get('live'), ensure_ascii=False)}")
        return 0

    if action == "threads":
        payload = console_threads()
        if fmt == "json":
            print(json.dumps(payload, ensure_ascii=False, indent=2))
        else:
            for name, entry in payload["threads"].items():
                marker = "*" if name == payload.get("active_task") else " "
                print(f"{marker} {name}: {entry.get('label', '')} "
                      f"[turns={entry.get('turns', '?')}] {entry.get('url', '')}")
        return 0

    if action == "selfcheck":
        result = console_selfcheck(wait_budget=args.wait_budget)
        if fmt == "json":
            print(json.dumps(result, ensure_ascii=False, indent=2))
        else:
            if result.get("ok"):
                print("SELFCHECK PASSED")
                print(f"answer: {result['answer'][:200]}")
                for name, value in result["gates"].items():
                    print(f"  gate {name}: {json.dumps(value, ensure_ascii=False)}")
                print(f"elapsed: {result['elapsed_s']}s")
            else:
                print("SELFCHECK FAILED")
                print(f"  gate: {result.get('gate')}")
                print(f"  error: {result.get('error')}")
                if result.get("evidence"):
                    print(f"  evidence: {result['evidence']}")
        return 0 if result.get("ok") else 1

    print("usage: perplexity console {ask,status,threads,selfcheck} ...")
    return 2


def cmd_search(args):
    """Single search."""
    from ..search import search, deep_research, model_council, step_by_step
    modes = {"search": search, "deep_research": deep_research,
             "model_council": model_council, "step_by_step": step_by_step}
    fn = modes.get(args.mode, search)
    result = fn(args.query, new_tab=None, verify=args.verify)
    if args.format == "json":
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print(f"URL: {result.get('url', 'N/A')}")
        print(f"Answer:\n{result.get('answer', 'No answer')}")
        print(f"\nSources ({len(result.get('sources', []))}):")
        for i, s in enumerate(result.get("sources", []), 1):
            print(f"  {i}. {s.get('text', '')[:80]} → {s.get('href', '')}")

        # Show verification results only in human-readable mode. JSON mode
        # must remain one valid JSON document on stdout.
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

    return 1 if result.get("error") else 0


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
              delay=args.delay, verify=args.verify)


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
    # Imported here rather than at module scope to keep CLI startup lazy, in
    # line with the other driver imports in this file.
    from ..drivers import DRIVER_REGISTRY

    backends = sorted(DRIVER_REGISTRY)

    parser = argparse.ArgumentParser(
        prog="perplexity",
        description="Perplexity Toolkit — automate Perplexity AI search",
    )
    parser.add_argument("-w", "--wait", type=float, help="Search wait time (seconds)")
    parser.add_argument("-r", "--retries", type=int, help="Max retries")
    parser.add_argument("--session-prefix", help="Task-scoped WebBridge session prefix")
    # Choices come from the registry so this flag can never advertise a backend
    # that is not actually implemented, and an unknown value is rejected here
    # rather than silently falling back to webbridge.
    parser.add_argument(
        "-b", "--backend", choices=backends,
        help=f"Driver backend ({', '.join(backends)})",
    )
    parser.set_defaults(verify=True)
    parser.add_argument("--verify", dest="verify", action="store_true",
                        help="Verify sources and answer quality (default)")
    parser.add_argument("--no-verify", dest="verify", action="store_false",
                        help="Skip source and answer-quality verification")
    parser.add_argument("-v", "--verbose", action="store_true", help="Enable DEBUG logging")
    sub = parser.add_subparsers(dest="command")

    # search
    p = sub.add_parser("search", help="Single search")
    p.add_argument("query", help="Search query")
    p.add_argument("-m", "--mode", default="search",
                   choices=["search", "deep_research", "model_council", "step_by_step"])
    p.add_argument("-f", "--format", default="text", choices=["text", "json"])
    p.add_argument("--verify", dest="verify", action="store_true", default=argparse.SUPPRESS,
                   help=argparse.SUPPRESS)
    p.add_argument("--no-verify", dest="verify", action="store_false", default=argparse.SUPPRESS,
                   help=argparse.SUPPRESS)

    # batch
    p = sub.add_parser("batch", help="Batch search")
    p.add_argument("queries", nargs="*", help="Inline queries")
    p.add_argument("-i", "--input", help="Input file (json/csv/txt)")
    p.add_argument("-o", "--output", default="batch_results.json")
    p.add_argument("-m", "--mode", default="search")
    p.add_argument("-r", "--resume", action="store_true")
    p.add_argument("--progress", default=".batch_progress")
    p.add_argument("-d", "--delay", type=float, default=3.0)
    p.add_argument("--verify", dest="verify", action="store_true", default=argparse.SUPPRESS,
                   help=argparse.SUPPRESS)
    p.add_argument("--no-verify", dest="verify", action="store_false", default=argparse.SUPPRESS,
                   help=argparse.SUPPRESS)

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

    # route
    p = sub.add_parser("route", help="Select CLI or direct-browser route")
    p.add_argument("request", help="Original user wording, not a search query")
    p.add_argument("-f", "--format", default="json", choices=["json", "text"])

    # console
    p = sub.add_parser("console", help="Resident Perplexity console (fixed tab/group)")
    csub = p.add_subparsers(dest="console_action")
    pc = csub.add_parser("ask", help="Ask the console; creates or continues a task thread")
    pc.add_argument("query")
    pc.add_argument("--task", default="default", help="Task name; one task = one thread")
    pc.add_argument("--new-thread", action="store_true",
                    help="Start a new Perplexity thread in the same tab")
    pc.add_argument("-f", "--format", default="text", choices=["text", "json"])
    pc.add_argument("--wait", dest="wait_budget", type=float, default=120.0,
                    help="Completion budget in seconds")
    pc.add_argument("--file", action="append", metavar="PATH",
                    help="Attach a local file to the message (repeatable; <=8MB)")
    pc = csub.add_parser("status", help="Show console state and live tab readback")
    pc.add_argument("-f", "--format", default="text", choices=["text", "json"])
    pc = csub.add_parser("threads", help="List known task threads")
    pc.add_argument("-f", "--format", default="text", choices=["text", "json"])
    pc = csub.add_parser("selfcheck", help="Run the full gate pipeline on a canned query")
    pc.add_argument("-f", "--format", default="text", choices=["text", "json"])
    pc.add_argument("--wait", dest="wait_budget", type=float, default=120.0)
    pc = csub.add_parser("models", help="List selectable Perplexity models")
    pc.add_argument("-f", "--format", default="text", choices=["text", "json"])
    pc = csub.add_parser("model", help="Switch the composer model (verified readback)")
    pc.add_argument("name")
    pc.add_argument("-f", "--format", default="text", choices=["text", "json"])

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
    if args.session_prefix:
        cfg_overrides["session_prefix"] = args.session_prefix
    if cfg_overrides:
        set_config(**cfg_overrides)

    # Setup logging
    level = logging.DEBUG if args.verbose else logging.INFO
    logging.basicConfig(level=level, format="%(name)s %(levelname)s: %(message)s")

    if args.command == "route":
        return cmd_route(args)
    elif args.command == "console":
        return cmd_console(args)
    elif args.command == "search":
        return cmd_search(args)
    elif args.command == "batch":
        cmd_batch(args)
    elif args.command == "aggregate":
        cmd_aggregate(args)
    elif args.command == "history":
        cmd_history(args)
    else:
        parser.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
