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

import base64
import datetime
import json
import logging
import mimetypes
import os
import re
import time
from pathlib import Path
from typing import Any, Callable, Optional

from . import console_judge
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
  const area = ce ? (ce.closest('form') || ce.parentElement.parentElement.parentElement.parentElement) : document.body;
  const modelBtns = Array.from(area.querySelectorAll('button')).filter(b => b.getAttribute('aria-haspopup') === 'menu' && (b.getAttribute('aria-label') || '').length > 0 && !/添加文件或工具|草稿/.test(b.getAttribute('aria-label')));
  const modelBtn = modelBtns.length ? modelBtns[modelBtns.length - 1] : null;
  return JSON.stringify({
    url: location.href,
    title: document.title,
    composer: ce ? String(ce.innerText || '') : '',
    model: modelBtn ? (modelBtn.getAttribute('aria-label') || '') : '',
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

_JS_MODEL_BTN = r"""(() => {
  const ce = document.querySelector('[contenteditable]');
  const area = ce ? (ce.closest('form') || ce.parentElement.parentElement.parentElement.parentElement) : document.body;
  const cands = Array.from(area.querySelectorAll('button')).filter(b => b.getAttribute('aria-haspopup') === 'menu' && (b.getAttribute('aria-label') || '').length > 0 && !/添加文件或工具|草稿/.test(b.getAttribute('aria-label')));
  const b = cands.length ? cands[cands.length - 1] : null;
  if (!b) return JSON.stringify({found: false});
  const r = b.getBoundingClientRect();
  return JSON.stringify({found: true, label: b.getAttribute('aria-label') || String(b.innerText || '').trim(),
    expanded: b.getAttribute('aria-expanded') === 'true',
    x: Math.round(r.x + r.width / 2), y: Math.round(r.y + r.height / 2)});
})()"""

_JS_MODEL_MENU = r"""(() => {
  const menu = document.querySelector('[role=menu]');
  if (!menu) return JSON.stringify({open: false, rows: []});
  const rows = Array.from(menu.querySelectorAll('[data-radix-collection-item], [role^=menuitem]')).map(r => {
    const b = r.getBoundingClientRect();
    const parts = String(r.innerText || '').split('\n').map(s => s.trim()).filter(Boolean);
    return {name: parts[0] || '', badges: parts.slice(1),
            role: r.getAttribute('role'), checked: r.getAttribute('aria-checked') === 'true',
            submenu: r.getAttribute('role') === 'menuitem',
            x: Math.round(b.x + b.width / 2), y: Math.round(b.y + b.height / 2),
            vis: !!r.offsetParent};
  });
  return JSON.stringify({open: true, rows: rows});
})()"""

_JS_CHIPS = r"""(() => {
  const labels = Array.from(document.querySelectorAll('button'))
    .map(b => b.getAttribute('aria-label') || '')
    .filter(a => a.indexOf('移除 ') === 0)
    .map(a => a.slice(3));
  return JSON.stringify({attachments: labels});
})()"""

_JS_INJECT_FILE = r"""(() => {
  const input = document.querySelector('input[type=file]');
  if (!input) return JSON.stringify({ok: false, why: 'no-input'});
  const bin = atob(__B64__);
  const arr = new Uint8Array(bin.length);
  for (let i = 0; i < bin.length; i++) arr[i] = bin.charCodeAt(i);
  const dt = new DataTransfer();
  dt.items.add(new File([arr], __NAME__, {type: __MIME__}));
  input.files = dt.files;
  input.dispatchEvent(new Event('change', {bubbles: true}));
  return JSON.stringify({ok: true, count: input.files.length, size: arr.length});
})()"""


# ──────────────────────────────────────────────────────────────
# Errors
# ──────────────────────────────────────────────────────────────

class ConsoleError(RuntimeError):
    """A console gate failed. Carries the gate name, evidence path and a
    machine-readable error code so callers (intent layer) can pick a
    recovery recipe per failure class."""

    def __init__(self, gate: str, message: str, *,
                 gates: Optional[dict] = None,
                 evidence: Optional[str] = None,
                 code: Optional[str] = None):
        super().__init__(f"[{gate}] {message}")
        self.gate = gate
        self.message = message
        self.gates = gates or {}
        self.evidence = evidence
        self.code = code


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
        "pending": None,
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
        for key in ("version", "session", "group_title", "active_task", "pending"):
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
# Staged turn ("pending") ledger
#
# Granular commands (fill → submit → wait → extract) compose through this
# record: fill stages it with the pre-submit baselines, submit/wait/extract
# consume it. It is what makes step-by-step intent-driven use safe — each
# step knows exactly which turn it belongs to.
# ──────────────────────────────────────────────────────────────

PENDING_TTL = 1800.0  # seconds a staged turn stays valid


def _pending(state: dict) -> dict:
    p = state.get("pending")
    return p if isinstance(p, dict) else {}


def _pending_age_seconds(p: dict) -> float:
    ts = p.get("filled_at")
    if not ts:
        return 0.0
    try:
        dt = datetime.datetime.strptime(ts, "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=datetime.timezone.utc)
        return (datetime.datetime.now(datetime.timezone.utc) - dt).total_seconds()
    except ValueError:
        return 0.0


def _pending_require(state: dict, *, stage: str) -> dict:
    p = _pending(state)
    if not p or not p.get("query"):
        raise ConsoleError(
            "pending",
            f"no staged turn for `{stage}`; run `perplexity console fill \"...\"` first",
            code="pending.missing")
    if stage in ("submit", "wait") and _pending_age_seconds(p) > PENDING_TTL:
        raise ConsoleError(
            "pending",
            f"staged turn is stale (>{int(PENDING_TTL / 60)} min old); re-run `console fill`",
            code="pending.stale")
    if stage == "submit" and p.get("submitted_at"):
        raise ConsoleError(
            "pending",
            "this turn was already submitted; use `console wait` / `console extract`, "
            "or `console fill` to stage a new turn",
            code="pending.already-submitted")
    if stage == "wait" and not p.get("submitted_at"):
        raise ConsoleError(
            "pending",
            "turn not submitted yet; run `console submit` first",
            code="pending.not-submitted")
    return p


def _log_run(op: str, *, ok: bool = True, gate: Optional[str] = None,
             url: Optional[str] = None, elapsed: Optional[float] = None) -> None:
    """Append one compact line to runs.jsonl (best-effort observability)."""
    try:
        path = console_home() / "runs.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        rec: dict = {"ts": _now_iso(), "op": op, "ok": bool(ok)}
        if gate:
            rec["gate"] = gate
        if url:
            rec["url"] = url
        if elapsed is not None:
            rec["elapsed_s"] = round(elapsed, 1)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except OSError:
        pass


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
        raise ConsoleError("attach", f"list_tabs returned {type(resp).__name__}",
                           code="attach.bad-response")
    blob = json.dumps(resp, ensure_ascii=False).lower()
    if "no extension connected" in blob:
        raise ConsoleError("attach", "WebBridge extension is not connected",
                           code="attach.disconnected")
    data = resp.get("data")
    if not isinstance(data, dict):
        data = {}
    if resp.get("ok") is False or data.get("success") is False or resp.get("error"):
        raise ConsoleError("attach", f"list_tabs failed: {resp.get('error') or resp}",
                           code="attach.bad-response")
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


def _cdp_click(driver: Any, x: int, y: int, *, sleep: Callable[[float], None]) -> None:
    """Trusted mouse click via CDP — required for Radix portal menus, where
    synthetic .click() does nothing (menu items are portals outside the
    composer subtree; plain button chips DO respond to synthetic clicks)."""
    driver.cdp("Input.dispatchMouseEvent", {"type": "mouseMoved", "x": x, "y": y})
    sleep(0.08)
    driver.cdp("Input.dispatchMouseEvent",
               {"type": "mousePressed", "x": x, "y": y, "button": "left", "clickCount": 1})
    sleep(0.12)
    driver.cdp("Input.dispatchMouseEvent",
               {"type": "mouseReleased", "x": x, "y": y, "button": "left", "clickCount": 1})


def _press_escape(driver: Any, *, sleep: Callable[[float], None]) -> None:
    for kind in ("keyDown", "keyUp"):
        driver.cdp("Input.dispatchKeyEvent",
                   {"type": kind, "key": "Escape", "code": "Escape",
                    "windowsVirtualKeyCode": 27, "nativeVirtualKeyCode": 27})
        sleep(0.12)


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
            raise ConsoleError("attach", "navigate did not create a session tab",
                               code="attach.no-tab")
        return {"created": created, "tab_count": 1, "url": target_url}

    current = (_info(driver).get("url") or "")
    if not _url_matches(current, target_url):
        driver.navigate(target_url, new_tab=False)
        sleep(cfg.page_load_wait)
        current = (_info(driver).get("url") or "")
        if not _url_matches(current, target_url):
            raise ConsoleError(
                "attach", f"tab did not reach {target_url!r} (at {current!r})",
                code="attach.navigate")
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
        evidence=_evidence(driver), code="fill.not-committed")


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
    qn = _normalize(query)
    sync = _info(driver)
    if (_normalize(sync.get("composer")) != qn
            or sync.get("submit_button") != "enabled"):
        refill = _gate_fill(driver, query, sleep=sleep)
        actions.append({"action": "refill-before-submit", "result": refill["method"],
                        "composer_before": refill.get("composer_before", "")[:80]})
    else:
        actions.append({"action": "composer-verified", "result": "equal"})

    # Freshness pass: one final replace right before sending so the editor
    # state is committed at submit time. An attachment can keep the submit
    # button enabled while the text state lags, which once produced a
    # file-only submission (observed live 2026-09-21).
    driver.fill("[contenteditable]", query)
    sleep(FILL_SETTLE)
    fresh = _info(driver)
    if (_normalize(fresh.get("composer")) != qn
            or fresh.get("submit_button") != "enabled"):
        refill = _gate_fill(driver, query, sleep=sleep)
        actions.append({"action": "refill-after-refresh", "result": refill["method"]})
    else:
        actions.append({"action": "composer-refresh", "result": "ok"})

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

    last = _info(driver).get("lastBubble") or ""
    raise ConsoleError(
        "submit",
        f"no new user turn observed after submit attempts (last bubble: {str(last)[:80]!r})",
        evidence=_evidence(driver), code="submit.no-turn")


def _gate_complete(driver: Any, base_studied: int, *,
                   base_prose_count: int = 0,
                   wait_budget: float, poll: float,
                   sleep: Callable[[float], None]) -> dict:
    """Gate 3: the NEW turn's answer must appear, then settle.

    A stale answer from the previous turn must never satisfy this gate: the
    turn-scoped answer count (``proseCount``) has to grow beyond the
    pre-submit baseline before stability counts (observed live 2026-09-21:
    a slow file-bearing answer let a naive stability check extract the
    previous turn's answer).
    """
    new_seen = False
    done_seen = False
    stable = 0
    last_len = -1
    deadline = time.monotonic() + max(wait_budget, 0.0)
    while True:
        info = _info(driver)
        if int(info.get("studied") or 0) > base_studied:
            done_seen = True
        if int(info.get("proseCount") or 0) > base_prose_count:
            new_seen = True
        length = int(info.get("lastProseLen") or 0)
        stable = stable + 1 if (new_seen and length > 0 and length == last_len) else 0
        last_len = length
        if new_seen and stable >= 1 and done_seen:
            break
        if new_seen and stable >= 2:
            break
        if time.monotonic() >= deadline:
            break
        sleep(max(poll, 0.01))

    method = "pill+stable" if done_seen else ("stable-only" if stable >= 2 else "none")
    ok = new_seen and last_len > 0 and stable >= 1
    result = {"ok": ok, "new_seen": new_seen, "done_seen": done_seen, "stable": stable,
              "chars": last_len, "method": method}
    if not ok:
        result["evidence"] = _evidence(driver)
        raise ConsoleError(
            "complete",
            f"new turn's answer never settled (new_seen={new_seen}, method={method})",
            gates={"complete": result}, code="complete.timeout")
    return result


# ──────────────────────────────────────────────────────────────
# Main entry points
# ──────────────────────────────────────────────────────────────

# ──────────────────────────────────────────────────────────────
# Model selector + file attachments
# ──────────────────────────────────────────────────────────────

def _model_button(driver: Any) -> dict:
    btn = _js(driver, _JS_MODEL_BTN, {})
    return btn if isinstance(btn, dict) else {}


def _open_model_menu(driver: Any, *, sleep: Callable[[float], None],
                     attempts: int = 2) -> dict:
    """Open the model selector menu and return its rows (menu left OPEN).

    The menu is a Radix portal: synthetic .click() does nothing, a trusted
    CDP mouse click works (verified live 2026-09-21).
    """
    button = _model_button(driver)
    if not button.get("found"):
        raise ConsoleError("model", "model selector button not found in the composer",
                           code="model.no-button")
    menu = _js(driver, _JS_MODEL_MENU, {})
    for _ in range(attempts):
        if isinstance(menu, dict) and menu.get("open"):
            break
        _cdp_click(driver, button["x"], button["y"], sleep=sleep)
        sleep(1.2)
        menu = _js(driver, _JS_MODEL_MENU, {})
    if not isinstance(menu, dict) or not menu.get("open"):
        raise ConsoleError("model", "model menu did not open after CDP clicks",
                           code="model.menu-failed")
    return {"button": button, "rows": menu.get("rows") or []}


def _close_model_menu(driver: Any, *, sleep: Callable[[float], None]) -> bool:
    menu = _js(driver, _JS_MODEL_MENU, {})
    if not (isinstance(menu, dict) and menu.get("open")):
        return True
    _press_escape(driver, sleep=sleep)
    sleep(0.4)
    menu = _js(driver, _JS_MODEL_MENU, {})
    return not (isinstance(menu, dict) and menu.get("open"))


def _clean_rows(rows: list) -> list:
    return [{"name": r.get("name") or "", "badges": r.get("badges") or [],
             "checked": bool(r.get("checked")), "submenu": bool(r.get("submenu")),
             "x": r.get("x"), "y": r.get("y")}
            for r in rows]


def _ensure_console_tab(driver: Any, state: dict, *, cfg: Config,
                        sleep: Callable[[float], None]) -> None:
    """Make sure the console session has a tab (no navigation if one exists)."""
    if _tabs(driver):
        return
    driver.navigate(BASE_URL, new_tab=True, group_title=state["group_title"])
    sleep(cfg.page_load_wait)


def console_models(*, config: Optional[Config] = None, driver: Any = None,
                   sleep: Callable[[float], None] = time.sleep) -> dict:
    """List the selectable models (name/badges/checked) and the current one."""
    cfg = config or get_config()
    state = load_state()
    drv = driver or _make_driver(cfg, state)
    _ensure_console_tab(drv, state, cfg=cfg, sleep=sleep)
    opened = _open_model_menu(drv, sleep=sleep)
    rows = _clean_rows(opened["rows"])
    closed = _close_model_menu(drv, sleep=sleep)
    current = next((r["name"] for r in rows if r["checked"]), None) \
        or opened["button"].get("label") or ""
    return {"ok": True, "current": current, "models": rows, "menu_closed": closed}


def console_set_model(name: str, *, config: Optional[Config] = None, driver: Any = None,
                      sleep: Callable[[float], None] = time.sleep) -> dict:
    """Switch the composer's model and VERIFY the new label before success."""
    cfg = config or get_config()
    state = load_state()
    drv = driver or _make_driver(cfg, state)
    _ensure_console_tab(drv, state, cfg=cfg, sleep=sleep)
    wanted = _normalize(name).casefold()
    if not wanted:
        raise ConsoleError("model", "empty model name")

    def find_row(rows):
        exact = [r for r in rows if _normalize(r.get("name", "")).casefold() == wanted]
        if not exact:
            exact = [r for r in rows if wanted in _normalize(r.get("name", "")).casefold()]
        return exact[0] if exact else None

    before = _model_button(drv).get("label")
    opened = _open_model_menu(drv, sleep=sleep)
    row = find_row(opened["rows"])
    if row is None:
        available = ", ".join(r.get("name", "?") for r in opened["rows"])
        _close_model_menu(drv, sleep=sleep)
        raise ConsoleError("model", f"model {name!r} not found; available: {available}",
                           code="model.not-found")
    if row.get("submenu"):
        _close_model_menu(drv, sleep=sleep)
        raise ConsoleError("model", f"{row.get('name')!r} is a submenu entry (not supported yet)",
                           code="model.submenu")

    label = ""
    for attempt in (1, 2):
        _cdp_click(drv, row["x"], row["y"], sleep=sleep)
        sleep(1.5)
        _close_model_menu(drv, sleep=sleep)
        label = _model_button(drv).get("label") or ""
        if wanted in _normalize(label).casefold():
            return {"ok": True, "from": before, "to": label, "attempts": attempt}
        if attempt == 1:
            opened = _open_model_menu(drv, sleep=sleep)
            row = find_row(opened["rows"]) or row
    raise ConsoleError("model", f"switch to {name!r} not verified (selector shows {label!r})",
                       evidence=_evidence(drv), code="model.verify-failed")


MAX_INJECT_BYTES = 8 * 1024 * 1024  # larger files need the chrome://extensions file-access route


def _inject_file(driver: Any, path: str) -> dict:
    """Attach a local file by building it in-page (DataTransfer + File).

    This bypasses Chrome's per-extension file access (off by default and not
    toggleable by the extension itself); the WebBridge ``upload`` action
    needs that permission, plain in-page File construction does not.
    """
    p = Path(path).expanduser()
    if not p.is_file():
        raise ConsoleError("file", f"file not found: {path}", code="file.not-found")
    data = p.read_bytes()
    if len(data) > MAX_INJECT_BYTES:
        raise ConsoleError(
            "file",
            f"{p.name} is {len(data)} bytes; in-page injection limit is {MAX_INJECT_BYTES} bytes "
            "(for larger files enable 'Allow access to file URLs' for the Kimi extension)",
            code="file.too-large")
    mime = mimetypes.guess_type(str(p))[0] or "application/octet-stream"
    code = (_JS_INJECT_FILE
            .replace("__B64__", json.dumps(base64.b64encode(data).decode("ascii")))
            .replace("__NAME__", json.dumps(p.name))
            .replace("__MIME__", json.dumps(mime)))
    res = _js(driver, code, {})
    if not isinstance(res, dict) or not res.get("ok"):
        raise ConsoleError("file", f"injection failed for {p.name}: {res}",
                           code="file.inject-failed")
    return {"name": p.name, "size": len(data), "mime": mime}


def _gate_files(driver: Any, files: list, *, sleep: Callable[[float], None],
                wait: float = 30.0, poll: float = 1.0) -> dict:
    """Gate: every requested file must be attached AND shown as a chip.

    Idempotent: files already present as chips are skipped, so injecting the
    same set twice (e.g. attach-then-fill, or a recovery re-run) never
    duplicates attachments.
    """
    chips0 = _js(driver, _JS_CHIPS, {})
    current = chips0.get("attachments") if isinstance(chips0, dict) else []
    injected = []
    for f in files:
        name = Path(f).expanduser().name
        if name in (current or []):
            injected.append({"name": name, "size": None, "mime": None,
                             "already_attached": True})
            continue
        injected.append(_inject_file(driver, f))
    names = [i["name"] for i in injected]
    deadline = time.monotonic() + wait
    chips: Any = []
    while True:
        payload = _js(driver, _JS_CHIPS, {})
        chips = payload.get("attachments") if isinstance(payload, dict) else []
        if all(n in (chips or []) for n in names):
            break
        if time.monotonic() >= deadline:
            raise ConsoleError("file", f"attachment chips not verified for {names} (saw {chips})",
                               evidence=_evidence(driver), code="file.chip-missing")
        sleep(max(poll, 0.05))
    # let the app finish processing the fresh upload before any composer work
    sleep(2.5)
    return {"ok": True, "injected": injected, "chips": chips}


def _ensure_task_context(drv: Any, state: dict, *, task: str, new_thread: bool,
                         cfg: Config, sleep: Callable[[float], None]) -> dict:
    """Attach to the task's thread (or home for a new thread) and verify."""
    thread = state["threads"].get(task) or {}
    target = BASE_URL if new_thread else (thread.get("url") or BASE_URL)
    attach_res = attach(drv, state, target, cfg=cfg, sleep=sleep)
    pre = _info(drv)
    if new_thread and HREF_MATCH_SLACK in (pre.get("url") or ""):
        # A new task must start from home, not inside another thread.
        drv.navigate(BASE_URL, new_tab=False)
        sleep(cfg.page_load_wait)
        pre = _info(drv)
        if HREF_MATCH_SLACK in (pre.get("url") or ""):
            raise ConsoleError("attach", "still inside a thread after new-thread navigation",
                               evidence=_evidence(drv), code="attach.still-thread")
    return {"attach": {"ok": True, **attach_res}, "pre": pre, "target": target}


def _stage_fill(drv: Any, state: dict, cfg: Config, query: str, *,
                task: str, new_thread: bool, files: Optional[list],
                sleep: Callable[[float], None]) -> dict:
    """Stage a turn: ensure context, attach files, fill+verify, record pending.

    Shared by ``console_ask``/``console_send``/``console_fill`` — one
    implementation, three surfaces.
    """
    ctx = _ensure_task_context(drv, state, task=task, new_thread=new_thread,
                               cfg=cfg, sleep=sleep)
    gates: dict = {"attach": ctx["attach"]}
    file_names = [Path(f).expanduser().name for f in (files or [])]
    file_paths = [str(Path(f).expanduser()) for f in (files or [])]
    if files:
        gates["files"] = _gate_files(drv, files, sleep=sleep)

    try:
        gates["fill"] = _gate_fill(drv, query, sleep=sleep)
    except ConsoleError as fill_exc:
        if fill_exc.gate != "fill":
            raise
        # Bounded self-heal: a desynced editor (DOM shows text, React state
        # empty) is reset by one page reload; the gate is then retried once.
        logger.warning("fill gate failed (%s); reloading page once and retrying",
                       fill_exc.message)
        reload_url = _info(drv).get("url") or ctx["target"]
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

    pre = ctx["pre"]
    pending = {
        "task": task,
        "query": query,
        "new_thread": bool(new_thread),
        "files": file_names,
        "file_paths": file_paths,
        "url": _info(drv).get("url") or "",
        "base_bubbles": int(pre.get("bubbles") or 0),
        "base_studied": int(pre.get("studied") or 0),
        "base_prose_count": int(pre.get("proseCount") or 0),
        "filled_at": _now_iso(),
        "status": "filled",
    }
    state["pending"] = pending
    state["active_task"] = task
    save_state(state)
    return {"ok": True, "gates": gates, "pending": pending}


def _submit_with_recovery(drv: Any, cfg: Config, query: str, base_bubbles: int, *,
                          files: Optional[list],
                          submit_timeout: float, submit_recheck_timeout: float,
                          poll_interval: float,
                          sleep: Callable[[float], None],
                          context_gates: Optional[dict] = None,
                          gates_out: Optional[dict] = None,
                          expect_url: Optional[str] = None) -> dict:
    """Submit gate with bounded recovery (delayed ownership → reload+retry).

    The recovery is duplicate-safe: it only re-submits after a delayed
    ownership recheck proves the turn was NOT actually sent.
    """
    if expect_url:
        current = _info(drv).get("url") or ""
        if not _url_matches(current, expect_url):
            raise ConsoleError(
                "pending",
                f"page moved: staged on {expect_url!r}, currently at {current!r}; "
                "run `console fill` again",
                code="pending.page-moved")
    try:
        return _gate_submit(drv, query, base_bubbles,
                            timeout=submit_timeout, poll=poll_interval, sleep=sleep)
    except ConsoleError as submit_exc:
        if submit_exc.gate != "submit":
            raise
        # First, give a slow/duplicated ownership read one more chance so a
        # late-but-correct turn never gets sent twice.
        if _wait_ownership(drv, query, base_bubbles, timeout=submit_recheck_timeout,
                           poll=poll_interval, sleep=sleep):
            return {"ok": True, "mechanism": "delayed-ownership",
                    "actions": [{"action": "late-ownership", "result": "ok"}]}
        # Bounded recovery: a mis-sent state (e.g. file-only submission /
        # stale editor state) is cleared by one page reload; re-inject
        # files and retry the submit gate exactly once.
        logger.warning("submit gate failed (%s); reloading and retrying once",
                       submit_exc.message)
        reload_url = _info(drv).get("url") or BASE_URL
        drv.navigate(reload_url, new_tab=False)
        sleep(max(cfg.page_load_wait, 4.0))
        files_retry = None
        if files:
            files_retry = _gate_files(drv, files, sleep=sleep)
            if gates_out is not None:
                gates_out["files_retry"] = files_retry
        try:
            result = _gate_submit(drv, query, base_bubbles,
                                  timeout=submit_timeout, poll=poll_interval, sleep=sleep)
            result["recovered"] = "reload"
            return result
        except ConsoleError as exc2:
            merge = {"submit_first_error": {"message": submit_exc.message,
                                            "evidence": submit_exc.evidence}}
            if files_retry is not None:
                merge["files_retry"] = files_retry
            if context_gates:
                merge.update(context_gates)
            exc2.gates = merge
            raise


def _extract_step(drv: Any, state: dict, cfg: Config, *,
                  sleep: Callable[[float], None],
                  pending: Optional[dict] = None,
                  gates_out: Optional[dict] = None) -> dict:
    """Expand, extract the turn-scoped answer + sources, update bookkeeping.

    With ``pending`` the thread record is updated and the staged turn is
    cleared (exactly once); without it this is a read-only ad-hoc extract.
    """
    gates = gates_out if gates_out is not None else {}

    expand = _js(drv, _JS_EXPAND, "none")
    if expand == "clicked":
        sleep(1.0)
    gates["expand"] = expand

    prose = _js(drv, _JS_PROSE, {})
    if (not isinstance(prose, dict) or not prose.get("found")
            or not _normalize(prose.get("text"))):
        gates["extract"] = {"ok": False}
        raise ConsoleError("extract", "latest answer text is empty",
                           gates=gates, evidence=_evidence(drv), code="extract.empty")
    gates["extract"] = {"ok": True, "chars": len(prose["text"])}

    file_names = list((pending or {}).get("files") or [])
    if file_names:
        payload = _js(drv, _JS_CHIPS, {})
        remaining = payload.get("attachments") if isinstance(payload, dict) else []
        gates["send"] = {"chips_cleared": not any(n in (remaining or []) for n in file_names)}

    sources = _js(drv, _JS_SOURCES, [])
    if not isinstance(sources, list):
        sources = []

    post = _info(drv)
    url = post.get("url") or ""
    if pending:
        task_name = pending.get("task") or "default"
        q = pending.get("query") or ""
        now = _now_iso()
        if HREF_MATCH_SLACK in url:
            entry = dict(state["threads"].get(task_name) or {})
            entry.update({"url": url, "last_used_at": now})
            if pending.get("new_thread") or not entry.get("created_at") or "url" not in entry:
                entry["created_at"] = now
                entry["label"] = _normalize(q)[:80]
                entry["turns"] = 1
            else:
                entry["turns"] = int(entry.get("turns") or 0) + 1
            state["threads"][task_name] = entry
        state["active_task"] = task_name
        state["pending"] = None  # the staged turn is consumed
    save_state(state)

    return {
        "ok": True,
        "answer": prose["text"],
        "raw_answer": prose.get("raw", prose["text"]),
        "sources": sources,
        "url": url,
        "title": post.get("title") or "",
        "model": post.get("model") or "",
    }


def console_ask(query: str, *, task: str = "default", new_thread: bool = False,
                files: Optional[list] = None,
                wait_budget: float = DEFAULT_WAIT, poll_interval: float = POLL_INTERVAL,
                submit_timeout: float = SUBMIT_TIMEOUT,
                submit_recheck_timeout: float = 6.0,
                judge: Optional[bool] = None,
                config: Optional[Config] = None, driver: Any = None,
                sleep: Callable[[float], None] = time.sleep) -> dict:
    """Ask the resident console one question, with every step verified.

    Composite of the granular steps (fill → submit → wait → extract), each
    backed by the same gate implementation the step commands use. One task =
    one Perplexity thread; without ``new_thread`` the task's thread is
    continued as a follow-up.
    """
    cfg = config or get_config()
    state = load_state()
    drv = driver or _make_driver(cfg, state)
    started = time.monotonic()

    staged = _stage_fill(drv, state, cfg, query, task=task, new_thread=new_thread,
                         files=files, sleep=sleep)
    gates = dict(staged["gates"])
    pending = staged["pending"]

    gates["submit"] = _submit_with_recovery(
        drv, cfg, query, pending["base_bubbles"], files=files,
        submit_timeout=submit_timeout, submit_recheck_timeout=submit_recheck_timeout,
        poll_interval=poll_interval, sleep=sleep, context_gates=gates, gates_out=gates)

    gates["complete"] = _gate_complete(drv, pending["base_studied"],
                                       base_prose_count=pending["base_prose_count"],
                                       wait_budget=wait_budget, poll=poll_interval, sleep=sleep)

    out = _extract_step(drv, state, cfg, sleep=sleep, pending=pending, gates_out=gates)
    judgment = console_judge.judge_extraction(query, out["answer"], flag=judge)
    _log_run("ask", ok=True, url=out["url"],
             elapsed=time.monotonic() - started)

    return {
        "ok": True,
        "answer": out["answer"],
        "judge": judgment,
        "raw_answer": out["raw_answer"],
        "sources": out["sources"],
        "url": out["url"],
        "title": out["title"],
        "model": out["model"],
        "attachments": pending["files"],
        "task": task,
        "session": state["session"],
        "new_thread": new_thread,
        "gates": gates,
        "elapsed_s": round(time.monotonic() - started, 1),
    }


# ──────────────────────────────────────────────────────────────
# Granular steps (intent composition surface)
# ──────────────────────────────────────────────────────────────

def console_open(target: str, *, new_thread: bool = False,
                 config: Optional[Config] = None, driver: Any = None,
                 sleep: Callable[[float], None] = time.sleep) -> dict:
    """Attach the console tab to a task thread (by name) or a direct URL."""
    cfg = config or get_config()
    state = load_state()
    drv = driver or _make_driver(cfg, state)
    if target.startswith("http"):
        tabs = _tabs(drv)
        if tabs:
            drv.navigate(target, new_tab=False)
        else:
            drv.navigate(target, new_tab=True, group_title=state["group_title"])
        sleep(cfg.page_load_wait)
        info = _info(drv)
        if not _url_matches(info.get("url") or "", target):
            raise ConsoleError("attach", f"tab did not reach {target!r}",
                               evidence=_evidence(drv), code="attach.navigate")
        _log_run("open", ok=True, url=info.get("url"))
        return {"ok": True, "url": info.get("url") or "", "target": target,
                "new_thread": False}
    ctx = _ensure_task_context(drv, state, task=target, new_thread=new_thread,
                               cfg=cfg, sleep=sleep)
    state["active_task"] = target
    save_state(state)
    _log_run("open", ok=True, url=ctx["pre"].get("url"))
    return {"ok": True, "url": ctx["pre"].get("url") or "", "target": target,
            "new_thread": new_thread, "attach": ctx["attach"]}


def _reload_console_tab(cfg: Config, state: dict, *,
                        sleep: Callable[[float], None],
                        driver: Any = None) -> None:
    """Reload the console's current tab (used by Jev-directed recovery)."""
    drv = driver or _make_driver(cfg, state)
    current = _info(drv).get("url") or BASE_URL
    drv.navigate(current, new_tab=False)
    sleep(max(cfg.page_load_wait, 4.0))


def _run_with_jev_recovery(step: str, run: Callable[[], dict], *,
                           judge: Optional[bool], cfg: Config, state: dict,
                           sleep: Callable[[float], None],
                           driver: Any = None) -> dict:
    """Run a step; on failure with judging on, let Jev pick ONE bounded remedy.

    Concept from browser-use/jev-ultrafast: Jev chooses among offered
    operations, code owns execution and safety. Scope limits here:

    - only safe, non-send steps (fill / wait / extract) — send paths stay
      advisory-only so an automatic retry can never double-send;
    - the remedy set is fixed {retry-step, reload-and-retry, wait-longer,
      escalate} and exactly ONE extra attempt is executed;
    - the retried run passes every original gate again; any non-ok decision
      (escalate / unavailable / skipped) re-raises the original error.
    """
    try:
        return run()
    except ConsoleError as exc:
        if step not in ("fill", "wait", "extract"):
            raise
        decision = console_judge.route_hint(exc.gate, exc.code, exc.message, flag=judge)
        route = decision.get("route") if decision.get("status") == "ok" else None
        if route not in ("retry-step", "reload-and-retry", "wait-longer"):
            raise  # escalate / unavailable / skipped -> original error stands
        logger.warning("jev-directed recovery: %s (confidence %.2f) after %s",
                       route, decision.get("confidence") or 0.0, exc.code or exc.gate)
        if route == "reload-and-retry":
            _reload_console_tab(cfg, state, sleep=sleep, driver=driver)
        elif route == "wait-longer":
            sleep(6.0)
        try:
            result = run()
        except ConsoleError as exc2:
            exc2.gates = {"jev_recovery": {"route": route, "applied": True,
                                           "attempts": 1, "first_error": exc.message},
                          **exc2.gates}
            raise
        if isinstance(result, dict):
            gates = result.setdefault("gates", {})
            if isinstance(gates, dict):
                gates["jev_recovery"] = {"route": route,
                                         "confidence": decision.get("confidence"),
                                         "applied": True, "attempts": 1}
        return result


def console_fill(query: str, *, task: str = "default", new_thread: bool = False,
                 files: Optional[list] = None,
                 judge: Optional[bool] = None,
                 config: Optional[Config] = None, driver: Any = None,
                 sleep: Callable[[float], None] = time.sleep) -> dict:
    """Stage a turn: attach files, fill the composer, verify — no send."""
    cfg = config or get_config()
    state = load_state()
    drv = driver or _make_driver(cfg, state)

    def _once() -> dict:
        staged = _stage_fill(drv, state, cfg, query, task=task, new_thread=new_thread,
                             files=files, sleep=sleep)
        _log_run("fill", ok=True, url=staged["pending"].get("url"))
        return {"ok": True, "gates": staged["gates"], "pending": staged["pending"]}

    return _run_with_jev_recovery("fill", _once, judge=judge, cfg=cfg, state=state,
                                  sleep=sleep, driver=drv)


def console_submit(*, config: Optional[Config] = None, driver: Any = None,
                   sleep: Callable[[float], None] = time.sleep,
                   submit_timeout: float = SUBMIT_TIMEOUT,
                   submit_recheck_timeout: float = 6.0,
                   poll_interval: float = POLL_INTERVAL) -> dict:
    """Submit the staged turn (composer re-verified; bounded recovery)."""
    cfg = config or get_config()
    state = load_state()
    drv = driver or _make_driver(cfg, state)
    pending = _pending_require(state, stage="submit")
    gates: dict = {}
    gates["submit"] = _submit_with_recovery(
        drv, cfg, pending["query"], int(pending.get("base_bubbles") or 0),
        files=list(pending.get("file_paths") or []),
        submit_timeout=submit_timeout, submit_recheck_timeout=submit_recheck_timeout,
        poll_interval=poll_interval, sleep=sleep, context_gates={},
        gates_out=gates, expect_url=pending.get("url"))
    pending["submitted_at"] = _now_iso()
    pending["status"] = "submitted"
    state["pending"] = pending
    save_state(state)
    _log_run("submit", ok=True, url=pending.get("url"))
    return {"ok": True, "gates": gates, "pending_status": "submitted"}


def console_wait(*, config: Optional[Config] = None, driver: Any = None,
                 judge: Optional[bool] = None,
                 sleep: Callable[[float], None] = time.sleep,
                 wait_budget: float = DEFAULT_WAIT,
                 poll_interval: float = POLL_INTERVAL) -> dict:
    """Wait for the staged turn's answer to settle (turn-scoped completion)."""
    cfg = config or get_config()
    state = load_state()
    drv = driver or _make_driver(cfg, state)

    def _once() -> dict:
        pending = _pending_require(state, stage="wait")
        gate = _gate_complete(drv, int(pending.get("base_studied") or 0),
                              base_prose_count=int(pending.get("base_prose_count") or 0),
                              wait_budget=wait_budget, poll=poll_interval, sleep=sleep)
        pending["completed_at"] = _now_iso()
        pending["status"] = "completed"
        state["pending"] = pending
        save_state(state)
        _log_run("wait", ok=True, url=pending.get("url"))
        return {"ok": True, "gates": {"complete": gate}}

    return _run_with_jev_recovery("wait", _once, judge=judge, cfg=cfg, state=state,
                                  sleep=sleep, driver=drv)


def console_extract(*, config: Optional[Config] = None, driver: Any = None,
                    judge: Optional[bool] = None,
                    sleep: Callable[[float], None] = time.sleep) -> dict:
    """Extract the newest answer (+sources); consumes the staged turn."""
    cfg = config or get_config()
    state = load_state()
    drv = driver or _make_driver(cfg, state)

    def _once() -> dict:
        pending = _pending(state) or None
        gates: dict = {}
        out = _extract_step(drv, state, cfg, sleep=sleep, pending=pending, gates_out=gates)
        out["gates"] = gates
        out["task"] = (pending or {}).get("task") or state.get("active_task")
        out["judge"] = console_judge.judge_extraction(
            (pending or {}).get("query") or "", out.get("answer") or "", flag=judge)
        _log_run("extract", ok=True, url=out.get("url"))
        return out

    return _run_with_jev_recovery("extract", _once, judge=judge, cfg=cfg, state=state,
                                  sleep=sleep, driver=drv)


def console_send(query: str, *, task: str = "default", new_thread: bool = False,
                 files: Optional[list] = None,
                 config: Optional[Config] = None, driver: Any = None,
                 sleep: Callable[[float], None] = time.sleep,
                 submit_timeout: float = SUBMIT_TIMEOUT,
                 submit_recheck_timeout: float = 6.0,
                 poll_interval: float = POLL_INTERVAL) -> dict:
    """Stage and submit in one call (fill + submit); no wait/extract."""
    cfg = config or get_config()
    state = load_state()
    drv = driver or _make_driver(cfg, state)
    staged = _stage_fill(drv, state, cfg, query, task=task, new_thread=new_thread,
                         files=files, sleep=sleep)
    gates = dict(staged["gates"])
    pending = staged["pending"]
    gates["submit"] = _submit_with_recovery(
        drv, cfg, query, pending["base_bubbles"], files=files,
        submit_timeout=submit_timeout, submit_recheck_timeout=submit_recheck_timeout,
        poll_interval=poll_interval, sleep=sleep, context_gates=gates, gates_out=gates)
    pending["submitted_at"] = _now_iso()
    pending["status"] = "submitted"
    state["pending"] = pending
    save_state(state)
    _log_run("send", ok=True, url=pending.get("url"))
    return {"ok": True, "gates": gates, "pending": pending, "task": task}


def console_attach(files: list, *, config: Optional[Config] = None, driver: Any = None,
                   sleep: Callable[[float], None] = time.sleep) -> dict:
    """Attach local files to the composer (idempotent, chips verified)."""
    cfg = config or get_config()
    state = load_state()
    drv = driver or _make_driver(cfg, state)
    if not files:
        raise ConsoleError("file", "no files given", code="file.not-found")
    _ensure_console_tab(drv, state, cfg=cfg, sleep=sleep)
    gate = _gate_files(drv, files, sleep=sleep)
    pending = _pending(state)
    if not pending:
        pending = {"task": state.get("active_task") or "default", "query": None,
                   "new_thread": False, "files": [], "file_paths": [],
                   "url": _info(drv).get("url") or "", "filled_at": _now_iso(),
                   "status": "attached"}
    names = [i["name"] for i in gate["injected"]]
    pending["files"] = sorted(set(list(pending.get("files") or []) + names))
    pending["file_paths"] = sorted(set(list(pending.get("file_paths") or []) +
                                       [str(Path(f).expanduser()) for f in files]))
    state["pending"] = pending
    save_state(state)
    _log_run("attach", ok=True)
    return {"ok": True, "gates": {"files": gate}, "pending_files": pending["files"]}


def console_detach(name: str, *, config: Optional[Config] = None, driver: Any = None,
                   sleep: Callable[[float], None] = time.sleep) -> dict:
    """Remove one composer attachment by name (verified)."""
    cfg = config or get_config()
    state = load_state()
    drv = driver or _make_driver(cfg, state)
    code = ("""(() => { const removeBtn = Array.from(document.querySelectorAll('button'))"""
            """.find(b => (b.getAttribute('aria-label') || '') === __LABEL__); """
            """if (!removeBtn) return 'not-found'; removeBtn.click(); return 'clicked'; })()"""
            ).replace("__LABEL__", json.dumps(f"移除 {name}"))
    result = _js(drv, code, "not-found")
    if result != "clicked":
        raise ConsoleError("file", f"attachment {name!r} not found as a chip",
                           code="file.chip-missing")
    sleep(1.0)
    chips = _js(drv, _JS_CHIPS, {})
    remaining = chips.get("attachments") if isinstance(chips, dict) else []
    if name in (remaining or []):
        raise ConsoleError("file", f"attachment {name!r} still present after remove",
                           code="file.chip-missing")
    pending = _pending(state)
    if pending:
        pending["files"] = [n for n in (pending.get("files") or []) if n != name]
        pending["file_paths"] = [p for p in (pending.get("file_paths") or [])
                                 if Path(p).name != name]
        state["pending"] = pending
        save_state(state)
    _log_run("detach", ok=True)
    return {"ok": True, "detached": name, "chips": remaining or []}


def console_files(*, config: Optional[Config] = None, driver: Any = None,
                  sleep: Callable[[float], None] = time.sleep) -> dict:
    """List current composer attachments and the staged file set."""
    cfg = config or get_config()
    state = load_state()
    drv = driver or _make_driver(cfg, state)
    _ensure_console_tab(drv, state, cfg=cfg, sleep=sleep)
    chips = _js(drv, _JS_CHIPS, {})
    attachments = chips.get("attachments") if isinstance(chips, dict) else []
    pending = _pending(state)
    return {"ok": True, "chips": attachments,
            "pending_files": pending.get("files") or [],
            "pending_status": pending.get("status")}


def console_status(*, config: Optional[Config] = None, driver: Any = None) -> dict:
    """Read-only view of console state plus a live tab readback."""
    state = load_state()
    out = {
        "session": state["session"],
        "group_title": state["group_title"],
        "active_task": state.get("active_task"),
        "threads": state.get("threads") or {},
        "pending": state.get("pending"),
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
        "model": info.get("model"),
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
                      submit_recheck_timeout: float = 6.0,
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
                             submit_recheck_timeout=submit_recheck_timeout,
                             config=config, driver=driver, sleep=sleep)
    except ConsoleError as exc:
        return {"ok": False, "gate": exc.gate, "error": exc.message,
                "error_code": exc.code,
                "evidence": exc.evidence, "gates": exc.gates,
                "elapsed_s": round(time.monotonic() - started, 1)}
    return result
