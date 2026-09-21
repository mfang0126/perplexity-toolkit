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
        evidence=_evidence(driver))


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
            gates={"complete": result})
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
        raise ConsoleError("model", "model selector button not found in the composer")
    menu = _js(driver, _JS_MODEL_MENU, {})
    for _ in range(attempts):
        if isinstance(menu, dict) and menu.get("open"):
            break
        _cdp_click(driver, button["x"], button["y"], sleep=sleep)
        sleep(1.2)
        menu = _js(driver, _JS_MODEL_MENU, {})
    if not isinstance(menu, dict) or not menu.get("open"):
        raise ConsoleError("model", "model menu did not open after CDP clicks")
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
        raise ConsoleError("model", f"model {name!r} not found; available: {available}")
    if row.get("submenu"):
        _close_model_menu(drv, sleep=sleep)
        raise ConsoleError("model", f"{row.get('name')!r} is a submenu entry (not supported yet)")

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
                       evidence=_evidence(drv))


MAX_INJECT_BYTES = 8 * 1024 * 1024  # larger files need the chrome://extensions file-access route


def _inject_file(driver: Any, path: str) -> dict:
    """Attach a local file by building it in-page (DataTransfer + File).

    This bypasses Chrome's per-extension file access (off by default and not
    toggleable by the extension itself); the WebBridge ``upload`` action
    needs that permission, plain in-page File construction does not.
    """
    p = Path(path).expanduser()
    if not p.is_file():
        raise ConsoleError("file", f"file not found: {path}")
    data = p.read_bytes()
    if len(data) > MAX_INJECT_BYTES:
        raise ConsoleError(
            "file",
            f"{p.name} is {len(data)} bytes; in-page injection limit is {MAX_INJECT_BYTES} bytes "
            "(for larger files enable 'Allow access to file URLs' for the Kimi extension)")
    mime = mimetypes.guess_type(str(p))[0] or "application/octet-stream"
    code = (_JS_INJECT_FILE
            .replace("__B64__", json.dumps(base64.b64encode(data).decode("ascii")))
            .replace("__NAME__", json.dumps(p.name))
            .replace("__MIME__", json.dumps(mime)))
    res = _js(driver, code, {})
    if not isinstance(res, dict) or not res.get("ok"):
        raise ConsoleError("file", f"injection failed for {p.name}: {res}")
    return {"name": p.name, "size": len(data), "mime": mime}


def _gate_files(driver: Any, files: list, *, sleep: Callable[[float], None],
                wait: float = 30.0, poll: float = 1.0) -> dict:
    """Gate: every requested file must be injected AND shown as a chip."""
    injected = [_inject_file(driver, f) for f in files]
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
                               evidence=_evidence(driver))
        sleep(max(poll, 0.05))
    # let the app finish processing the fresh upload before any composer work
    sleep(2.5)
    return {"ok": True, "injected": injected, "chips": chips}


def console_ask(query: str, *, task: str = "default", new_thread: bool = False,
                files: Optional[list] = None,
                wait_budget: float = DEFAULT_WAIT, poll_interval: float = POLL_INTERVAL,
                submit_timeout: float = SUBMIT_TIMEOUT,
                submit_recheck_timeout: float = 6.0,
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

    file_names = [Path(f).expanduser().name for f in (files or [])]
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
    base_prose_count = int(pre.get("proseCount") or 0)
    try:
        gates["submit"] = _gate_submit(drv, query, base_bubbles,
                                       timeout=submit_timeout, poll=poll_interval, sleep=sleep)
    except ConsoleError as submit_exc:
        if submit_exc.gate != "submit":
            raise
        # First, give a slow/duplicated ownership read one more chance so a
        # late-but-correct turn never gets sent twice.
        if _wait_ownership(drv, query, base_bubbles, timeout=submit_recheck_timeout,
                           poll=poll_interval, sleep=sleep):
            gates["submit"] = {"ok": True, "mechanism": "delayed-ownership",
                               "actions": [{"action": "late-ownership", "result": "ok"}]}
        else:
            # Bounded recovery: a mis-sent state (e.g. file-only submission /
            # stale editor state) is cleared by one page reload; re-inject
            # files and retry the submit gate exactly once.
            logger.warning("submit gate failed (%s); reloading and retrying once",
                           submit_exc.message)
            reload_url = _info(drv).get("url") or target
            drv.navigate(reload_url, new_tab=False)
            sleep(max(cfg.page_load_wait, 4.0))
            if files:
                gates["files_retry"] = _gate_files(drv, files, sleep=sleep)
            try:
                gates["submit"] = _gate_submit(drv, query, base_bubbles,
                                               timeout=submit_timeout, poll=poll_interval,
                                               sleep=sleep)
                gates["submit"]["recovered"] = "reload"
            except ConsoleError as exc2:
                exc2.gates = {"submit_first_error": {"message": submit_exc.message,
                                                     "evidence": submit_exc.evidence},
                              **gates}
                raise
    gates["complete"] = _gate_complete(drv, base_studied,
                                       base_prose_count=base_prose_count,
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

    if files:
        payload = _js(drv, _JS_CHIPS, {})
        remaining = payload.get("attachments") if isinstance(payload, dict) else []
        gates["send"] = {"chips_cleared": not any(n in (remaining or []) for n in file_names)}

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
        "model": post.get("model") or "",
        "attachments": file_names,
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
                "evidence": exc.evidence, "gates": exc.gates,
                "elapsed_s": round(time.monotonic() - started, 1)}
    return result
