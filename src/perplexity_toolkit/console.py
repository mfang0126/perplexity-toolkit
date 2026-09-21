"""Resident Perplexity console — one long-lived tab/group with verified steps.

Design (live-mapped against the perplexity.ai web UI, 2026-09-21):

* One WebBridge session ("perplexity-console") owns one tab inside the group
  "Perplexity 控制台". Every console task shares that single tab; a *task*
  maps to one Perplexity thread (conversation) inside it.
* WebBridge session→tab mappings live only in the daemon's memory and are
  lost when the daemon restarts, so the durable console state is this
  module's JSON state file (session, group title, per-task thread URL).
  Attach-or-recreate reads it back: reuse the live tab when present,
  otherwise reopen the saved thread URL in a fresh tab of the same
  session/group. The Perplexity thread itself lives server-side, so the
  persisted URL restores continuity across browser and daemon restarts.
* Every step carries a readback gate. A gate that cannot be verified raises
  ConsoleError with a screenshot path — the console never reports an
  unverified step as success:
    1. fill      — composer text EQUALS the query. ``fill`` is this editor's
                   only reliable mutation path (CDP keys, execCommand and
                   DOM/range edits are reverted by the editor's controlled
                   re-render, and empty/whitespace fill values are silent
                   no-ops). A late async draft-restore can merge extra text
                   into the composer after the fill gate passed, so the
                   equality is re-verified and re-filled immediately before
                   submit (observed live 2026-09-21: merged draft + query
                   produced a polluted submission).
    2. submit    — a new user bubble exists whose text equals the query or
                   begins with it; "contains" alone proved too weak.
    3. complete  — the studied-pill count increased AND the turn-scoped
                   answer text stopped changing
    4. extract   — the latest answer (`div.prose`, turn-scoped) is non-empty
* Turn scoping: the answer is extracted from the LAST ``div.prose`` element,
  never from ``main.innerText`` (which spans the whole thread). The current
  UI can retain text in the composer after submission, so the composer is NOT
  used as the submit signal; user-bubble ownership is authoritative.
"""

from __future__ import annotations

import datetime
import json
import logging
import os
import re
import time
from pathlib import Path
from typing import Any, Callable, Optional

from .config import Config, get_config

logger = logging.getLogger(__name__)

DEFAULT_SESSION = "perplexity-console"
DEFAULT_GROUP_TITLE = "Perplexity 控制台"
BASE_URL = "https://www.perplexity.ai/"
HREF_MATCH_SLACK = "/search/"

DEFAULT_WAIT = 120.0          # seconds, completion budget
POLL_INTERVAL = 3.0           # seconds between completion samples
SUBMIT_TIMEOUT = 15.0         # seconds to observe the new user turn
FILL_SETTLE = 0.7             # seconds after a fill/CDP insert before readback
SELFCHECK_QUERY = "用一句话回答：1+1 等于几？"

# ──────────────────────────────────────────────────────────────
# JS snippets (markers are relied on by tests: 'user-bubble', 'cloneNode',
# "a[href]", '提交', 'dispatchEvent', "=== '展开'").
# ──────────────────────────────────────────────────────────────

_JS_INFO = r"""(() => {
  const btns = Array.from(document.querySelectorAll('button'));
  const bubbles = Array.from(document.querySelectorAll('[class*="user-bubble"]'))
    .filter(el => !String(el.className || '').includes('opacity-0'));
  const main = document.querySelector('main');
  const prose = main ? Array.from(main.querySelectorAll('div.prose')) : [];
  const ce = document.querySelector('[contenteditable]');
  return JSON.stringify({
    url: location.href,
    title: document.title,
    composer: ce ? String(ce.innerText || '') : '',
    submit_button: (() => { const b = document.querySelector('button[aria-label="提交"]'); return b ? (b.disabled ? 'disabled' : 'enabled') : 'missing'; })(),
    bubbles: bubbles.length,
    lastBubble: bubbles.length ? String(bubbles[bubbles.length - 1].innerText || '').slice(0, 300) : '',
    studied: btns.filter(b => String(b.innerText || '').includes('已研究')).length,
    generating: /正在|思考中|generating/i.test(document.body.textContent || ''),
    proseCount: prose.length,
    lastProseLen: prose.length ? String(prose[prose.length - 1].innerText || '').length : 0
  });
})()"""

_JS_PROSE = r"""(() => {
  const main = document.querySelector('main');
  if (!main) return JSON.stringify({found: false});
  const prose = Array.from(main.querySelectorAll('div.prose'));
  if (!prose.length) return JSON.stringify({found: false});
  const el = prose[prose.length - 1];
  const clone = el.cloneNode(true);
  clone.querySelectorAll('a[href], button').forEach(n => n.remove());
  return JSON.stringify({
    found: true,
    text: String(clone.innerText || '').trim(),
    raw: String(el.innerText || '').trim()
  });
})()"""

_JS_SOURCES = r"""(() => {
  const main = document.querySelector('main');
  if (!main) return JSON.stringify([]);
  const seen = new Set();
  const out = [];
  for (const a of main.querySelectorAll('a[href]')) {
    const href = a.href || '';
    if (!href.startsWith('http') || href.includes('perplexity.ai')) continue;
    if (seen.has(href)) continue;
    seen.add(href);
    out.push({text: String(a.textContent || '').trim().slice(0, 200), href: href});
  }
  return JSON.stringify(out);
})()"""

_JS_CLICK_SUBMIT = r"""(() => {
  const sels = ['button[aria-label="提交"]', 'button[aria-label="搜索"]', 'button[aria-label="Submit"]'];
  for (const s of sels) {
    const btn = document.querySelector(s);
    if (btn) { btn.click(); return 'clicked:' + s; }
  }
  const btns = Array.from(document.querySelectorAll('button'));
  const t = btns.find(b => { const x = String(b.innerText || '').trim(); return x === '搜索' || x === '提交' || x === 'Submit'; });
  if (t) { t.click(); return 'clicked:text'; }
  return 'no-button';
})()"""

_JS_ENTER_COMBO = r"""(() => {
  const el = document.querySelector('[contenteditable]');
  if (!el) return 'no input';
  el.dispatchEvent(new InputEvent('beforeinput', {inputType: 'insertText', data: '\n', bubbles: true, cancelable: true}));
  el.dispatchEvent(new KeyboardEvent('keydown', {key: 'Enter', code: 'Enter', keyCode: 13, which: 13, bubbles: true, cancelable: true}));
  el.dispatchEvent(new KeyboardEvent('keyup', {key: 'Enter', code: 'Enter', keyCode: 13, which: 13, bubbles: true, cancelable: true}));
  return 'enter dispatched';
})()"""

_JS_EXPAND = r"""(() => {
  const btns = Array.from(document.querySelectorAll('button'))
    .filter(b => String(b.innerText || '').trim() === '展开');
  if (!btns.length) return 'none';
  btns[btns.length - 1].click();
  return 'clicked';
})()"""


# ──────────────────────────────────────────────────────────────
# Errors
# ──────────────────────────────────────────────────────────────

class ConsoleError(RuntimeError):
    """A console gate failed. Carries the gate name and evidence path."""

    def __init__(self, gate: str, message: str, *,
                 gates: Optional[dict] = None,
                 evidence: Optional[str] = None):
        super().__init__(f"[{gate}] {message}")
        self.gate = gate
        self.message = message
        self.gates = gates or {}
        self.evidence = evidence


# ──────────────────────────────────────────────────────────────
# State file
# ──────────────────────────────────────────────────────────────

def console_home() -> Path:
    """Console state directory (override with PERPLEXITY_CONSOLE_HOME)."""
    return Path(os.environ.get("PERPLEXITY_CONSOLE_HOME", "~/.perplexity-console")).expanduser()


def state_path() -> Path:
    return console_home() / "state.json"


def _default_state() -> dict:
    return {
        "version": 1,
        "session": DEFAULT_SESSION,
        "group_title": DEFAULT_GROUP_TITLE,
        "active_task": None,
        "threads": {},
    }


def load_state() -> dict:
    """Read the console state file; missing/corrupt files yield defaults."""
    path = state_path()
    state = _default_state()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return state
    if isinstance(data, dict):
        for key in ("version", "session", "group_title", "active_task"):
            if key in data:
                state[key] = data[key]
        if isinstance(data.get("threads"), dict):
            state["threads"] = data["threads"]
    return state


def save_state(state: dict) -> None:
    """Atomically persist the console state."""
    path = state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)


# ──────────────────────────────────────────────────────────────
# Small helpers
# ──────────────────────────────────────────────────────────────

def _now_iso() -> str:
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _normalize(s: Any) -> str:
    return " ".join(str(s or "").split())


def _short(value: Any, limit: int = 140) -> str:
    try:
        return json.dumps(value, ensure_ascii=False)[:limit]
    except (TypeError, ValueError):
        return str(value)[:limit]


def _bubble_owns(bubble_text: str, query: str) -> bool:
    """True when a user bubble carries exactly this query (UI appends HH:MM).

    Equality or a query-prefix is required; a bubble that merely CONTAINS the
    query while carrying extra text is a polluted submission and must fail
    the gate (a merged draft + query once produced exactly that).
    """
    t = _normalize(bubble_text)
    t = re.sub(r"\s*\d{1,2}:\d{2}\s*$", "", t)
    q = _normalize(query)
    if not q:
        return False
    if t == q or t.startswith(q):
        return True
    # the bubble probe truncates at 300 chars: tolerate truncation for long queries
    return len(t) >= 280 and q[:120] in t


def _url_matches(current: str, target: str) -> bool:
    cur = (current or "").rstrip("/")
    tgt = (target or "").rstrip("/")
    if not tgt:
        return bool(cur)
    if tgt == BASE_URL.rstrip("/"):
        return cur == tgt  # home means no thread path
    return cur.startswith(tgt)


def _js(driver: Any, code: str, default: Any = None) -> Any:
    """Run a JS snippet and normalize its result.

    String results are attempted as JSON; a plain non-JSON string (e.g.
    'clicked:button[...]') is a legitimate value and is returned as-is.
    Only empty/None results fall back to ``default``. (The old behavior
    silently replaced plain-string results with the default, which made a
    successful submit click look like 'no-button'.)
    """
    value = driver.evaluate(code)
    if isinstance(value, str):
        if value == "":
            return default
        try:
            return json.loads(value)
        except (json.JSONDecodeError, TypeError):
            return value
    return default if value is None else value


def _info(driver: Any) -> dict:
    value = _js(driver, _JS_INFO, {})
    return value if isinstance(value, dict) else {}


def _tabs(driver: Any) -> list:
    """Session tabs via list_tabs, or ConsoleError on bridge failure."""
    resp = driver.list_tabs()
    if not isinstance(resp, dict):
        raise ConsoleError("attach", f"list_tabs returned {type(resp).__name__}")
    blob = json.dumps(resp, ensure_ascii=False).lower()
    if "no extension connected" in blob:
        raise ConsoleError("attach", "WebBridge extension is not connected")
    data = resp.get("data")
    if not isinstance(data, dict):
        data = {}
    if resp.get("ok") is False or data.get("success") is False or resp.get("error"):
        raise ConsoleError("attach", f"list_tabs failed: {resp.get('error') or resp}")
    tabs = data.get("tabs")
    return tabs if isinstance(tabs, list) else []


def _evidence(driver: Any) -> Optional[str]:
    """Best-effort failure screenshot; returns the path (never raises)."""
    try:
        path = console_home() / "evidence" / (
            datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-console.png")
        path.parent.mkdir(parents=True, exist_ok=True)
        driver.screenshot(str(path))
        return str(path)
    except Exception as exc:  # noqa: BLE001 — evidence must never mask the gate
        logger.debug("evidence screenshot failed: %s", exc)
        return None


def _make_driver(cfg: Config, state: dict) -> Any:
    from .drivers.webbridge import WebBridgeDriver
    return WebBridgeDriver(cfg.webbridge_url, session=state.get("session") or DEFAULT_SESSION)


# ──────────────────────────────────────────────────────────────
# Attach-or-recreate
# ──────────────────────────────────────────────────────────────

def attach(driver: Any, state: dict, target_url: str, *,
           cfg: Optional[Config] = None,
           sleep: Callable[[float], None] = time.sleep) -> dict:
    """Ensure the console session has one tab, sitting on ``target_url``.

    Reuses the live tab when the session still has one; otherwise opens a
    fresh tab for ``target_url`` (a saved thread URL or the home page) in the
    same session with the console group title.
    """
    cfg = cfg or get_config()
    tabs = _tabs(driver)
    created = False

    if not tabs:
        driver.navigate(target_url, new_tab=True, group_title=state["group_title"])
        sleep(cfg.page_load_wait)
        created = True
        if not _tabs(driver):
            raise ConsoleError("attach", "navigate did not create a session tab")
        return {"created": created, "tab_count": 1, "url": target_url}

    current = (_info(driver).get("url") or "")
    if not _url_matches(current, target_url):
        driver.navigate(target_url, new_tab=False)
        sleep(cfg.page_load_wait)
        current = (_info(driver).get("url") or "")
        if not _url_matches(current, target_url):
            raise ConsoleError(
                "attach", f"tab did not reach {target_url!r} (at {current!r})")
    return {"created": created, "tab_count": len(_tabs(driver)), "url": current}


# ──────────────────────────────────────────────────────────────
# Gate helpers
# ──────────────────────────────────────────────────────────────

def _gate_fill(driver: Any, query: str, *, sleep: Callable[[float], None],
               attempts: int = 3) -> dict:
    """Gate 1: the composer must EQUAL the query AND the editor state must
    have committed — proven by the submit button being enabled.

    ``fill`` is the editor's only reliable mutation path and is
    clear-and-insert (replace), so a polluted composer is repaired by
    re-filling, not by keyboard/DOM clearing (those get reverted by the
    editor's controlled re-render; empty fill values are silent no-ops).
    The DOM text and the editor's internal React state can diverge; the
    submit button's disabled flag exposes the internal state (disabled =
    state empty even if the DOM shows text), so both must agree.
    """
    qn = _normalize(query)
    detail: dict = {"composer_before": _composer(driver), "tries": []}
    for attempt in range(1, attempts + 1):
        driver.click("[contenteditable]")
        sleep(0.4)
        resp = driver.fill("[contenteditable]", query)
        sleep(FILL_SETTLE)
        info = _info(driver)
        after = _normalize(info.get("composer"))
        btn = info.get("submit_button")
        if after == qn and btn == "enabled":
            detail["tries"].append({"n": attempt, "resp": _short(resp), "btn": btn})
            return {"ok": True, "attempts": attempt, "method": "fill", **detail}
        if after == qn and btn in ("disabled", "missing"):
            # slow React commit? re-read once after a longer settle
            sleep(1.2)
            info = _info(driver)
            after2 = _normalize(info.get("composer"))
            btn2 = info.get("submit_button")
            if after2 == qn and btn2 == "enabled":
                detail["tries"].append({"n": attempt, "resp": _short(resp), "btn": btn2,
                                        "via": "delayed-commit"})
                return {"ok": True, "attempts": attempt, "method": "fill", **detail}
            after, btn = after2, btn2
        if after == "" and btn in ("disabled", "missing"):
            # fill was a silent no-op on an empty editor: trusted insertText
            driver.cdp("Input.insertText", {"text": query})
            sleep(FILL_SETTLE)
            info = _info(driver)
            after = _normalize(info.get("composer"))
            btn = info.get("submit_button")
            if after == qn and btn == "enabled":
                detail["tries"].append({"n": attempt, "resp": _short(resp), "btn": btn,
                                        "via": "cdp-insertText"})
                return {"ok": True, "attempts": attempt, "method": "cdp-insertText", **detail}
        detail["tries"].append({"n": attempt, "resp": _short(resp),
                                "after": after[:60], "btn": btn})
        detail["last_after"] = after[:100]
    raise ConsoleError(
        "fill",
        f"composer/editor state never committed (left={detail.get('last_after', '')!r}, "
        f"btn={detail['tries'][-1].get('btn')!r})",
        evidence=_evidence(driver))


def _composer(driver: Any) -> str:
    return _normalize(_info(driver).get("composer"))


def _wait_ownership(driver: Any, query: str, base_bubbles: int, *,
                    timeout: float, poll: float,
                    sleep: Callable[[float], None]) -> bool:
    """Gate 2 core: a new user turn OWNING this query must appear."""
    deadline = time.monotonic() + timeout
    while True:
        info = _info(driver)
        if (int(info.get("bubbles") or 0) > base_bubbles
                and _bubble_owns(info.get("lastBubble") or "", query)):
            return True
        if time.monotonic() >= deadline:
            return False
        sleep(max(poll, 0.05))


def _gate_submit(driver: Any, query: str, base_bubbles: int, *,
                 timeout: float, poll: float,
                 sleep: Callable[[float], None]) -> dict:
    """Gate 2: verify the composer once more, then submit.

    A late async draft-restore can merge extra text into the composer AFTER
    gate 1 passed, so equality is re-checked here and repaired by re-filling
    before any submit action runs. Submission itself goes through the submit
    button (multi-selector), falling back to the Enter combo.
    """
    actions = []
    sync = _info(driver)
    if (_normalize(sync.get("composer")) != _normalize(query)
            or sync.get("submit_button") != "enabled"):
        refill = _gate_fill(driver, query, sleep=sleep)
        actions.append({"action": "refill-before-submit", "result": refill["method"],
                        "composer_before": refill.get("composer_before", "")[:80]})
    else:
        actions.append({"action": "composer-verified", "result": "equal"})

    clicked = _js(driver, _JS_CLICK_SUBMIT, "no-button")
    actions.append({"action": "click-submit-button", "result": clicked})
    if "clicked" not in str(clicked):
        # UI transition gaps can transiently unmount the button: retry once
        sleep(0.8)
        retry = _js(driver, _JS_CLICK_SUBMIT, "no-button")
        actions.append({"action": "click-submit-button-retry", "result": retry})
        clicked = retry
    if _wait_ownership(driver, query, base_bubbles, timeout=timeout, poll=poll, sleep=sleep):
        mechanism = "button" if "clicked" in str(clicked) else "unexpected-none"
        return {"ok": True, "mechanism": mechanism, "actions": actions}

    combo = _js(driver, _JS_ENTER_COMBO, "no input")
    actions.append({"action": "enter-combo", "result": combo})
    if _wait_ownership(driver, query, base_bubbles, timeout=timeout, poll=poll, sleep=sleep):
        return {"ok": True, "mechanism": "combo", "actions": actions}

    raise ConsoleError("submit", "no new user turn observed after submit attempts",
                       evidence=_evidence(driver))


def _gate_complete(driver: Any, base_studied: int, *,
                   wait_budget: float, poll: float,
                   sleep: Callable[[float], None]) -> dict:
    """Gate 3: studied-pill count up AND the latest answer text stable.

    Falls back to text stability alone when the studied pill never appears.
    """
    done_seen = False
    stable = 0
    last_len = -1
    deadline = time.monotonic() + max(wait_budget, 0.0)
    while True:
        info = _info(driver)
        if int(info.get("studied") or 0) > base_studied:
            done_seen = True
        length = int(info.get("lastProseLen") or 0)
        stable = stable + 1 if (length > 0 and length == last_len) else 0
        last_len = length
        if stable >= 1 and done_seen:
            break
        if stable >= 2:
            break
        if time.monotonic() >= deadline:
            break
        sleep(max(poll, 0.01))

    method = "pill+stable" if done_seen else ("stable-only" if stable >= 2 else "none")
    ok = last_len > 0 and stable >= 1
    result = {"ok": ok, "done_seen": done_seen, "stable": stable,
              "chars": last_len, "method": method}
    if not ok:
        result["evidence"] = _evidence(driver)
        raise ConsoleError("complete", f"answer never settled (method={method})",
                           gates={"complete": result})
    return result


# ──────────────────────────────────────────────────────────────
# Main entry points
# ──────────────────────────────────────────────────────────────

def console_ask(query: str, *, task: str = "default", new_thread: bool = False,
                wait_budget: float = DEFAULT_WAIT, poll_interval: float = POLL_INTERVAL,
                submit_timeout: float = SUBMIT_TIMEOUT,
                config: Optional[Config] = None, driver: Any = None,
                sleep: Callable[[float], None] = time.sleep) -> dict:
    """Ask the resident console one question, with every step verified.

    Continuation model: one task = one Perplexity thread. Without
    ``new_thread`` an existing thread for ``task`` is continued as a
    follow-up; otherwise a new thread is started in the same tab.
    """
    cfg = config or get_config()
    state = load_state()
    drv = driver or _make_driver(cfg, state)
    gates: dict = {}
    started = time.monotonic()

    thread = state["threads"].get(task) or {}
    target = thread.get("url") or BASE_URL
    if new_thread:
        target = BASE_URL
    gates["attach"] = {"ok": True, **attach(drv, state, target, cfg=cfg, sleep=sleep)}

    pre = _info(drv)
    if new_thread and HREF_MATCH_SLACK in (pre.get("url") or ""):
        # A new task must start from home, not inside another thread.
        drv.navigate(BASE_URL, new_tab=False)
        sleep(cfg.page_load_wait)
        pre = _info(drv)
        if HREF_MATCH_SLACK in (pre.get("url") or ""):
            raise ConsoleError("attach", "still inside a thread after new-thread navigation",
                               gates=gates, evidence=_evidence(drv))

    try:
        gates["fill"] = _gate_fill(drv, query, sleep=sleep)
    except ConsoleError as fill_exc:
        if fill_exc.gate != "fill":
            raise
        # Bounded self-heal: a desynced editor (DOM shows text, React state
        # empty) is reset by one page reload; the gate is then retried once.
        logger.warning("fill gate failed (%s); reloading page once and retrying",
                       fill_exc.message)
        reload_url = _info(drv).get("url") or target
        drv.navigate(reload_url, new_tab=False)
        sleep(max(cfg.page_load_wait, 4.0))
        try:
            gates["fill"] = _gate_fill(drv, query, sleep=sleep)
            gates["fill"]["recovered"] = "reload"
        except ConsoleError as exc2:
            exc2.gates = {
                "fill_first_error": {"message": fill_exc.message,
                                     "evidence": fill_exc.evidence},
                **gates,
            }
            raise

    base_bubbles = int(pre.get("bubbles") or 0)
    base_studied = int(pre.get("studied") or 0)
    gates["submit"] = _gate_submit(drv, query, base_bubbles,
                                   timeout=submit_timeout, poll=poll_interval, sleep=sleep)
    gates["complete"] = _gate_complete(drv, base_studied,
                                       wait_budget=wait_budget, poll=poll_interval, sleep=sleep)

    expand = _js(drv, _JS_EXPAND, "none")
    if expand == "clicked":
        sleep(1.0)
    gates["expand"] = expand

    prose = _js(drv, _JS_PROSE, {})
    if (not isinstance(prose, dict) or not prose.get("found")
            or not _normalize(prose.get("text"))):
        evidence = _evidence(drv)
        gates["extract"] = {"ok": False}
        raise ConsoleError("extract", "latest answer text is empty",
                           gates=gates, evidence=evidence)
    gates["extract"] = {"ok": True, "chars": len(prose["text"])}

    sources = _js(drv, _JS_SOURCES, [])
    if not isinstance(sources, list):
        sources = []

    post = _info(drv)
    url = post.get("url") or ""
    now = _now_iso()
    if HREF_MATCH_SLACK in url:
        entry = dict(state["threads"].get(task) or {})
        entry.update({"url": url, "last_used_at": now})
        if new_thread or not entry.get("created_at") or "url" not in entry:
            entry["created_at"] = now
            entry["label"] = _normalize(query)[:80]
            entry["turns"] = 1
        else:
            entry["turns"] = int(entry.get("turns") or 0) + 1
        state["threads"][task] = entry
    state["active_task"] = task
    save_state(state)

    return {
        "ok": True,
        "answer": prose["text"],
        "raw_answer": prose.get("raw", prose["text"]),
        "sources": sources,
        "url": url,
        "title": post.get("title") or "",
        "task": task,
        "session": state["session"],
        "new_thread": new_thread,
        "gates": gates,
        "elapsed_s": round(time.monotonic() - started, 1),
    }


def console_status(*, config: Optional[Config] = None, driver: Any = None) -> dict:
    """Read-only view of console state plus a live tab readback."""
    state = load_state()
    out = {
        "session": state["session"],
        "group_title": state["group_title"],
        "active_task": state.get("active_task"),
        "threads": state.get("threads") or {},
        "live": None,
    }
    drv = driver or _make_driver(config or get_config(), state)
    try:
        tabs = _tabs(drv)
    except ConsoleError as exc:
        out["live"] = {"error": str(exc)}
        return out
    if not tabs:
        out["live"] = {"tabs": 0, "note": "no live tab in this session (attach will recreate)"}
        return out
    info = _info(drv)
    out["live"] = {
        "tabs": len(tabs),
        "url": info.get("url"),
        "title": info.get("title"),
        "on_thread": HREF_MATCH_SLACK in (info.get("url") or ""),
        "bubbles": info.get("bubbles"),
        "studied": info.get("studied"),
        "prose_count": info.get("proseCount"),
    }
    return out


def console_threads() -> dict:
    """List the recorded task threads."""
    state = load_state()
    return {
        "session": state["session"],
        "group_title": state["group_title"],
        "active_task": state.get("active_task"),
        "threads": state.get("threads") or {},
    }


def console_selfcheck(*, wait_budget: float = DEFAULT_WAIT,
                      poll_interval: float = POLL_INTERVAL,
                      submit_timeout: float = SUBMIT_TIMEOUT,
                      config: Optional[Config] = None, driver: Any = None,
                      sleep: Callable[[float], None] = time.sleep) -> dict:
    """Run the full gate pipeline against the console with a canned query.

    First run creates the ``selfcheck`` thread; later runs continue it as
    follow-ups, so both the new-thread and continuation paths get exercised
    across invocations. Prints per-gate verdicts for the caller.
    """
    started = time.monotonic()
    try:
        result = console_ask(SELFCHECK_QUERY, task="selfcheck", new_thread=False,
                             wait_budget=wait_budget, poll_interval=poll_interval,
                             submit_timeout=submit_timeout,
                             config=config, driver=driver, sleep=sleep)
    except ConsoleError as exc:
        return {"ok": False, "gate": exc.gate, "error": exc.message,
                "evidence": exc.evidence, "gates": exc.gates,
                "elapsed_s": round(time.monotonic() - started, 1)}
    return result
