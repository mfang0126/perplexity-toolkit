#!/usr/bin/env python3
"""Read-only four-state probe for the Perplexity console ``_JS_INFO`` snapshot.

Dumps the ``_JS_INFO`` state under the four composer states used to verify
the completion-determination predicate:

  1_before_submit_with_draft   after fill, draft present, NOT submitted
  2_during_generation          within ~3s of a real submit (generating)
  3_after_done_empty_composer  after the answer settles (composer cleared)
  4_after_done_with_draft      fill again after completion (draft present)

Snapshots are written to
``<console_home()>/evidence/probe_submit_states-<epoch>.json``
(default ``~/.perplexity-console/evidence/``) and each state is echoed to
stdout as one compact JSON line.

WARNING — 未获用户 GO 不得运行 (DO NOT RUN WITHOUT EXPLICIT USER GO):

  State 2 performs ONE real submission against perplexity.ai — an external
  side effect (a real question is sent to your Perplexity account). The
  script pauses before that submit and requires an interactive Enter at an
  explicit confirmation prompt. States 1/3/4 are read-only page reads plus
  composer fill. Do not run this script until the user has given GO.

Usage::

    python3 scripts/probe_submit_states.py --help
    python3 scripts/probe_submit_states.py --query "用一句话回答：1+1 等于几？"
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

# src-layout fallback: allow running from the repo without `pip install -e .`
_SRC = Path(__file__).resolve().parent.parent / "src"
if _SRC.is_dir() and str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from perplexity_toolkit.config import get_config  # noqa: E402
from perplexity_toolkit.console import (  # noqa: E402
    DEFAULT_WAIT,
    POLL_INTERVAL,
    SELFCHECK_QUERY,
    SUBMIT_TIMEOUT,
    ConsoleError,
    _JS_CLICK_SUBMIT,  # noqa: F401 — imported for signature-parity checks
    _ensure_console_tab,
    _gate_complete,
    _gate_fill,
    _gate_submit,
    _info,
    _make_driver,
    console_home,
    load_state,
)

STATE_KEYS = (
    "1_before_submit_with_draft",
    "2_during_generation",
    "3_after_done_empty_composer",
    "4_after_done_with_draft",
)


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _snapshot(driver) -> dict:
    """One _JS_INFO snapshot; ``.get()`` tolerates fields (e.g. stop_button)
    that a parallel change may not have added yet."""
    info = _info(driver)
    if not isinstance(info, dict):
        info = {"_error": "non-dict _JS_INFO result", "_raw": repr(info)}
    return {
        "ts": _now_iso(),
        "submit_button": info.get("submit_button"),
        "stop_button": info.get("stop_button"),   # may be absent (old _JS_INFO)
        "generating": info.get("generating"),
        "studied": info.get("studied"),
        "lastProseLen": info.get("lastProseLen"),
        "proseCount": info.get("proseCount"),
        "bubbles": info.get("bubbles"),
        "composer": info.get("composer") or "",
        "composer_len": len(info.get("composer") or ""),
        "url": info.get("url"),
        "info": info,  # full raw _JS_INFO payload
    }


def _emit(key: str, snap: dict) -> None:
    """One compact JSON line per state on stdout."""
    print(json.dumps({"state": key, "snapshot": snap},
                     ensure_ascii=False, separators=(",", ":")),
          flush=True)


def _write_evidence(states: dict, epoch: int) -> Path:
    out_dir = console_home() / "evidence"
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"probe_submit_states-{epoch}.json"
    path.write_text(
        json.dumps(states, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="probe_submit_states.py",
        description=(
            "Read-only four-state probe of _JS_INFO in the Perplexity console. "
            "State 2 performs ONE real submission (external side effect) behind "
            "an interactive confirmation."
        ),
        epilog=(
            "未获用户 GO 不得运行 (DO NOT RUN WITHOUT EXPLICIT USER GO): state 2 "
            "sends a real question to perplexity.ai; the script blocks on an "
            "Enter-to-continue prompt immediately before that submit. "
            "States 1/3/4 only read the page and fill the composer."
        ),
    )
    parser.add_argument("--query", default=SELFCHECK_QUERY,
                        help="probe query used for fill/submit (default: %(default)s)")
    parser.add_argument("--wait-budget", type=float, default=DEFAULT_WAIT,
                        help="completion budget in seconds (default: %(default)s)")
    parser.add_argument("--poll", type=float, default=POLL_INTERVAL,
                        help="poll interval in seconds (default: %(default)s)")
    parser.add_argument("--submit-timeout", type=float, default=SUBMIT_TIMEOUT,
                        help="submit ownership timeout in seconds (default: %(default)s)")
    args = parser.parse_args(argv)

    cfg = get_config()
    state = load_state()
    driver = _make_driver(cfg, state)
    _ensure_console_tab(driver, state, cfg=cfg, sleep=time.sleep)

    epoch = int(time.time())
    states: dict[str, dict] = {}
    rc = 0
    try:
        # Baselines captured pre-fill; _gate_complete needs them and the
        # probe bypasses the pending ledger on purpose (no state.json writes).
        pre = _info(driver)

        # ── State 1: draft in composer, nothing submitted ──────────────
        _gate_fill(driver, args.query, sleep=time.sleep)
        snap1 = _snapshot(driver)
        states["1_before_submit_with_draft"] = snap1
        _emit("1_before_submit_with_draft", snap1)

        # ── GO gate: state 2 requires a REAL submission ────────────────
        print("== 即将执行唯一一次真实 submit（外部副作用：真实提问已发送到 Perplexity）。"
              "未获用户 GO 请现在 Ctrl-C 退出。 ==", flush=True)
        input("已获用户 GO，按回车继续（state 2 将提交）… ")

        # ── State 2: within ~3s of submit, while generating ────────────
        t0 = time.monotonic()
        _gate_submit(driver, args.query, int(pre.get("bubbles") or 0),
                     timeout=args.submit_timeout, poll=args.poll,
                     sleep=time.sleep)
        time.sleep(max(0.0, 3.0 - (time.monotonic() - t0)))
        snap2 = _snapshot(driver)
        states["2_during_generation"] = snap2
        _emit("2_during_generation", snap2)

        # ── State 3: answer settled, composer expected empty ───────────
        _gate_complete(driver, int(pre.get("studied") or 0),
                       base_prose_count=int(pre.get("proseCount") or 0),
                       wait_budget=args.wait_budget, poll=args.poll,
                       sleep=time.sleep)
        snap3 = _snapshot(driver)
        states["3_after_done_empty_composer"] = snap3
        _emit("3_after_done_empty_composer", snap3)

        # ── State 4: fill again after completion, draft present ────────
        _gate_fill(driver, args.query, sleep=time.sleep)
        snap4 = _snapshot(driver)
        states["4_after_done_with_draft"] = snap4
        _emit("4_after_done_with_draft", snap4)

    except ConsoleError as exc:
        print(json.dumps({"error": "ConsoleError", "gate": exc.gate,
                          "code": exc.code, "message": exc.message,
                          "evidence": exc.evidence},
                         ensure_ascii=False, separators=(",", ":")),
              file=sys.stderr, flush=True)
        rc = 1
    except KeyboardInterrupt:
        print("\naborted by user (Ctrl-C); partial evidence will be written",
              file=sys.stderr, flush=True)
        rc = 130
    finally:
        # Evidence is written even for partial runs (any state captured so far).
        try:
            path = _write_evidence(states, epoch)
            print(json.dumps({"evidence": str(path),
                              "states_captured": sorted(states),
                              "expected": list(STATE_KEYS)},
                             ensure_ascii=False, separators=(",", ":")),
                  flush=True)
        except OSError as exc:
            print(json.dumps({"evidence_error": str(exc)},
                             ensure_ascii=False, separators=(",", ":")),
                  file=sys.stderr, flush=True)
            if rc == 0:
                rc = 1
    return rc


if __name__ == "__main__":
    sys.exit(main())
