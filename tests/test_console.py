"""Tests for the resident console: gates, attach, state, error paths.

These tests drive the console against a deterministic in-memory driver that
models the live-mapped perplexity.ai behavior:
- user bubbles append on submit; the studied count and last-answer text
  update when the answer lands;
- ``fill`` is the editor's mutation path and replaces content; empty or
  whitespace-only fill values are silent no-ops (observed live);
- a late async draft-restore can merge extra text into the composer after a
  successful fill (observed live: a merged draft+query produced a polluted
  submission — the console must refill before submitting);
- the model selector is a Radix portal: only trusted CDP mouse clicks open
  the menu / select rows (synthetic clicks do nothing);
- files attach by in-page File construction (DataTransfer + change event),
  and every attachment must surface as a "移除 <name>" chip.
"""
import json
import re
import sys; sys.path.insert(0, "src")

import pytest

from perplexity_toolkit import console
from perplexity_toolkit.config import Config
from perplexity_toolkit.console import (
    BASE_URL,
    ConsoleError,
    _bubble_owns,
    _now_iso,
    _url_matches,
    console_ask,
    console_attach,
    console_detach,
    console_extract,
    console_fill,
    console_models,
    console_selfcheck,
    console_send,
    console_set_model,
    console_status,
    console_submit,
    console_threads,
    console_wait,
    load_state,
    save_state,
)
from perplexity_toolkit.drivers.base import BrowserDriver
from perplexity_toolkit.commands.cli import build_parser, cmd_console


def make_config():
    return Config(page_load_wait=0.0)


def NOOP(_seconds):
    return None


class ConsoleFakeDriver(BrowserDriver):
    """Deterministic in-memory driver for console tests."""

    def __init__(self, *, share_tab=False, answer="答案 42。",
                 url=BASE_URL, button_submit_works=True,
                 composer_clears=False, studied_on_submit=True,
                 submit_disabled=False, fill_noop_first=0,
                 fill_appends=False, race_draft_after=None,
                 race_draft_text="旧草稿", force_btn_disabled=False,
                 btn_missing_first_click=0, desync_until_reload=False,
                 desync_recover_after=1, busy=False):
        self.url = url
        self.answer = answer
        self.share_tab = share_tab
        self.button_submit_works = button_submit_works
        self.composer_clears = composer_clears
        self.studied_on_submit = studied_on_submit
        self.submit_disabled = submit_disabled
        self.fill_noop_first = fill_noop_first
        self.fill_appends = fill_appends
        self.race_draft_after = race_draft_after
        self.race_draft_text = race_draft_text
        self.force_btn_disabled = force_btn_disabled
        self.btn_missing_first_click = btn_missing_first_click
        self.editor_desynced = desync_until_reload
        self._desync_recover_after = desync_recover_after
        self._nav_count = 0
        self.composer = ""
        self.bubbles = []
        self.studied = 0
        self.prose = []
        self.generating = False
        self.tab_url = url
        self.calls = []
        self.screenshots = []
        self._fill_noops = 0
        self._info_calls = 0
        self._race_applied = False
        self._submit_clicks = 0
        self.model = "Gemini 3.8 Flash"
        self.model_menu_open = False
        self.model_rows = [
            ("最佳", False),
            ("GPT-5.6 Terra", False),
            ("Gemini 3.8 Flash", False),
            ("Claude Sonnet 5", False),
            ("GPT-5.6 Sol", True),
            ("GLM 5.3", False),
        ]
        self.attachments = []
        self.inject_fail = False
        self.answer_delay_samples = 0
        self._pending_answer = False
        self._info_after_submit = 0
        self.misfire_until_reload = False
        self.pollute_first_fill = False
        self._polluted_once = False
        self.busy = busy   # still-generating page: busy stop + non-idle icon

    def _btn_state(self):
        """The submit button mirrors the editor's internal state."""
        if self.force_btn_disabled:
            return "disabled"
        if self.editor_desynced:
            return "disabled"
        return "enabled" if self.composer.strip() else "disabled"

    # -- BrowserDriver interface -------------------------------------------

    def _tab(self):
        return {"tabId": 1266525784, "url": self.tab_url, "title": "Perplexity",
                "groupTitle": "Perplexity 控制台", "borrowed": False}

    def list_tabs(self):
        self.calls.append(("list_tabs",))
        tabs = [self._tab()] if self.share_tab else []
        return {"ok": True, "data": {"success": True, "tabs": tabs}}

    def navigate(self, url, new_tab=True, group_title=""):
        self.calls.append(("navigate", url, new_tab, group_title))
        self.share_tab = True
        self.url = url.rstrip("/") or BASE_URL
        self.tab_url = self.url
        self._nav_count += 1
        if self._nav_count >= self._desync_recover_after:
            self.editor_desynced = False  # a reload resets the editor
        self.misfire_until_reload = False  # a reload resets the misfire mode
        if url.rstrip("/") == BASE_URL.rstrip("/"):
            self.bubbles = []
            self.prose = []
            self.studied = 0
            self.composer = ""
        return {"ok": True, "data": {"success": True, "url": url, "tabId": 1}}

    def snapshot(self):
        return {"data": {"tree": ""}}

    def click(self, selector):
        self.calls.append(("click", selector))
        return {"ok": True}

    def fill(self, selector, value):
        self.calls.append(("fill", selector, value))
        if not value.strip():
            # empty/whitespace fills are silent no-ops in the real editor
            return {"ok": True, "data": {"success": True, "mode": "contenteditable"}}
        if self._fill_noops < self.fill_noop_first:
            self._fill_noops += 1
            return {"ok": True, "data": {"success": True, "mode": "contenteditable"}}
        if self.pollute_first_fill and not self._polluted_once:
            # the first fill merges into existing content (draft-merge race);
            # every later fill replaces properly, so a re-fill heals it
            self._polluted_once = True
            self.composer = self.composer + value
        elif self.fill_appends:
            self.composer = self.composer + value
        else:
            self.composer = value
        return {"ok": True, "data": {"success": True, "mode": "contenteditable"}}

    def cdp(self, method, params=None):
        self.calls.append(("cdp", method, params))
        if method == "Input.insertText" and params:
            self.composer = params.get("text", "")
        elif method == "Input.dispatchMouseEvent" and params:
            if params.get("type") == "mousePressed":
                x = int(params.get("x") or 0)
                y = int(params.get("y") or 0)
                if not self.model_menu_open and abs(x - 845) < 5 and abs(y - 956) < 5:
                    self.model_menu_open = True
                elif self.model_menu_open:
                    for i, (name, sub) in enumerate(self.model_rows):
                        if abs(y - (600 + i * 36)) <= 12:
                            if not sub:
                                self.model = name
                            break
                    self.model_menu_open = False
        elif method == "Input.dispatchKeyEvent" and params and params.get("key") == "Escape":
            self.model_menu_open = False
        return {"ok": True}

    def evaluate(self, code, **kwargs):
        self.calls.append(("evaluate", code[:60]))
        if "cloneNode" in code:
            text = self.prose[-1] if self.prose else ""
            return {"found": bool(self.prose), "text": text, "raw": text}
        if "scrollTop" in code:
            # fake page is always scrolled to the bottom (live container:
            # div.scrollable-container.overflow-auto)
            return {"bottom": True, "height": 1000, "scrolled": False,
                    "container": "DIV.scrollable-container"}
        if "user-bubble" in code:
            self._info_calls += 1
            if (self.race_draft_after is not None and not self._race_applied
                    and self._info_calls >= self.race_draft_after):
                # late async draft-restore merges extra text into the composer
                self.composer = self.race_draft_text + "\n" + self.composer
                self._race_applied = True
            if self._pending_answer:
                self._info_after_submit += 1
                if self._info_after_submit >= self.answer_delay_samples:
                    self._pending_answer = False
                    if self.studied_on_submit:
                        self.studied += 1
                    self.prose.append(self.answer)
            return {
                "url": self.url,
                "title": "Perplexity",
                "composer": self.composer,
                "submit_button": self._btn_state(),
                "action_icon": BUSY_ICON if self.busy else IDLE_ICON,
                "stop_button": self.busy,
                "model": self.model,
                "bubbles": len(self.bubbles),
                "lastBubble": self.bubbles[-1] if self.bubbles else "",
                "studied": self.studied,
                "generating": self.generating,
                "proseCount": len(self.prose),
                "lastProseLen": len(self.prose[-1]) if self.prose else 0,
            }
        if "atob(" in code:
            if self.inject_fail:
                return {"ok": False, "why": "scripted-failure"}
            m = re.search(r'new File\(\[arr\],\s*("(?:[^"\\]|\\.)*")', code)
            name = json.loads(m.group(1)) if m else "file.bin"
            self.attachments.append(name)
            return {"ok": True, "count": len(self.attachments), "size": 1}
        if "[role=menu]" in code:
            if not self.model_menu_open:
                return {"open": False, "rows": []}
            rows = [{"name": n, "badges": [],
                     "role": "menuitem" if sub else "menuitemradio",
                     "checked": (n == self.model), "submenu": sub,
                     "x": 780, "y": 600 + i * 36, "vis": True}
                    for i, (n, sub) in enumerate(self.model_rows)]
            return {"open": True, "rows": rows}
        if "removeBtn" in code:
            m = re.search(r'===\s*("(?:[^"\\]|\\.)*")', code)
            label = json.loads(m.group(1)) if m else ""
            name = label[3:] if label.startswith("移除 ") else label
            if name in self.attachments:
                self.attachments.remove(name)
                return "clicked"
            return "not-found"
        if "移除 " in code:
            return {"attachments": list(self.attachments)}
        if "aria-haspopup" in code:
            return {"found": True, "label": self.model,
                    "expanded": self.model_menu_open, "x": 845, "y": 956}
        if "a[href]" in code:
            return [{"text": "src", "href": "https://example.com/x"}]
        if "提交" in code:
            self._submit_clicks += 1
            if self._submit_clicks <= self.btn_missing_first_click:
                return "no-button"  # transient UI gap: button not found
            if self.button_submit_works:
                self._submit()
            return "clicked:text"
        if "dispatchEvent" in code:
            self._submit()
            return "enter dispatched"
        if "=== '展开'" in code:
            return "none"
        return ""

    def _submit(self):
        if self.submit_disabled:
            return
        query = self.composer.strip()
        if not query:
            return
        if self.bubbles and self.bubbles[-1].startswith(query):
            return  # idempotent: this query is already the last turn
        if self.misfire_until_reload:
            # reproduce the live file-only misfire: the attachment (not the
            # text) gets sent until a page reload clears the bad state
            name = self.attachments[0] if self.attachments else "attachment"
            if not (self.bubbles and self.bubbles[-1].startswith(name)):
                self.bubbles.append(name + "\n13:40")
            self.attachments = []
            return
        self.bubbles.append(query + "\n13:40")
        self.attachments = []
        if self.composer_clears:
            self.composer = ""
        if self.answer_delay_samples:
            # a slow answer: the new prose shows up only after N info polls,
            # so a naive stability check on the PREVIOUS answer must not pass
            self._pending_answer = True
            self._info_after_submit = 0
        else:
            if self.studied_on_submit:
                self.studied += 1
            self.prose.append(self.answer)
        self.url = "https://www.perplexity.ai/search/fake-thread-1"
        self.tab_url = self.url

    def screenshot(self, path=None):
        self.calls.append(("screenshot", path))
        self.screenshots.append(path)
        return {"ok": True, "data": {"path": path}}

    def close(self):
        return {"ok": True}


@pytest.fixture(autouse=True)
def tmp_console_home(tmp_path, monkeypatch):
    monkeypatch.setenv("PERPLEXITY_CONSOLE_HOME", str(tmp_path))
    return tmp_path


def _ask(drv, query, *, task="default", new_thread=False, files=None, judge=None,
         wait_budget=1.0, poll_interval=0.01, submit_timeout=0.4, sleep=NOOP):
    return console_ask(query, task=task, new_thread=new_thread, files=files,
                       judge=judge,
                       wait_budget=wait_budget, poll_interval=poll_interval,
                       submit_timeout=submit_timeout, submit_recheck_timeout=0.1,
                       config=make_config(), driver=drv, sleep=sleep)


class TestConsoleAsk:
    def test_fresh_session_creates_tab_and_passes_all_gates(self):
        drv = ConsoleFakeDriver()
        res = _ask(drv, "用一句话回答：1+1 等于几？")
        assert res["ok"]
        assert res["answer"] == "答案 42。"
        assert res["gates"]["attach"]["created"] is True
        assert res["gates"]["fill"]["ok"]
        assert res["gates"]["fill"]["method"] == "fill"
        assert res["gates"]["submit"]["ok"]
        assert res["gates"]["submit"]["mechanism"] == "button"
        assert res["gates"]["submit"]["actions"][0]["action"] == "composer-verified"
        assert res["gates"]["complete"]["ok"]
        assert res["gates"]["extract"]["ok"]
        assert res["url"].startswith("https://www.perplexity.ai/search/")
        state = load_state()
        entry = state["threads"]["default"]
        assert entry["url"].endswith("/search/fake-thread-1")
        assert entry["turns"] == 1
        assert state["active_task"] == "default"
        creates = [c for c in drv.calls if c[0] == "navigate" and c[2] is True]
        assert len(creates) == 1
        assert creates[0][3] == "Perplexity 控制台"

    def test_fill_gate_replaces_residual_content(self):
        drv = ConsoleFakeDriver(share_tab=True, url=BASE_URL)
        drv.composer = "残留草稿"
        res = _ask(drv, "q-替换测试")
        assert res["gates"]["fill"]["ok"]
        assert res["gates"]["fill"]["method"] == "fill"
        assert res["gates"]["fill"]["composer_before"] == "残留草稿"
        assert drv.calls  # sanity

    def test_fill_retries_silent_noop_with_inserttext(self):
        drv = ConsoleFakeDriver(fill_noop_first=1)
        res = _ask(drv, "q-静默失败重试")
        assert res["gates"]["fill"]["ok"]
        assert res["gates"]["fill"]["method"] == "cdp-insertText"
        assert any(c[0] == "cdp" for c in drv.calls)

    def test_late_draft_merge_is_refilled_before_submit(self):
        drv = ConsoleFakeDriver(race_draft_after=4)
        res = _ask(drv, "q-草稿竞态")
        assert res["ok"]
        actions = [a["action"] for a in res["gates"]["submit"]["actions"]]
        assert actions[0] == "refill-before-submit"
        # the submission carried ONLY the query, never the merged draft
        assert drv.bubbles[0].split("\n")[0] == "q-草稿竞态"
        assert not any("旧草稿" in b for b in drv.bubbles)

    def test_fill_pollution_heals_by_refill(self):
        # The first fill merges into existing content (draft-merge race);
        # a later replace heals it and the submitted bubble carries ONLY the
        # query — no reload needed here.
        drv = ConsoleFakeDriver(share_tab=True, url=BASE_URL)
        drv.composer = "残留"
        drv.pollute_first_fill = True
        res = _ask(drv, "q-污染")
        assert res["ok"]
        assert res["gates"]["fill"]["composer_before"] == "残留"
        assert drv.bubbles and drv.bubbles[0].startswith("q-污染")
        assert not any("残留" in b for b in drv.bubbles)

    def test_submit_falls_back_to_enter_combo(self):
        drv = ConsoleFakeDriver(button_submit_works=False)
        res = _ask(drv, "q-提交兜底")
        actions = [a["action"] for a in res["gates"]["submit"]["actions"]]
        assert actions == ["composer-verified", "composer-refresh",
                           "click-submit-button", "enter-combo"]
        assert res["gates"]["submit"]["mechanism"] == "combo"
        assert res["gates"]["submit"]["ok"]

    def test_submit_click_retry_after_transition_gap(self):
        drv = ConsoleFakeDriver(btn_missing_first_click=1)
        res = _ask(drv, "q-点击重试")
        actions = [a["action"] for a in res["gates"]["submit"]["actions"]]
        assert actions == ["composer-verified", "composer-refresh",
                           "click-submit-button", "click-submit-button-retry"]
        assert res["gates"]["submit"]["mechanism"] == "button"

    def test_submit_misfire_recovers_after_reload(self, tmp_path):
        # reproduce the live file-only misfire: the first submits send only
        # the attachment until a page reload; the pipeline must recover and
        # end with exactly one correct owning turn
        f = tmp_path / "console-attach-test.txt"
        f.write_text("42", encoding="utf-8")
        state = load_state()
        state["threads"]["default"] = {
            "url": "https://www.perplexity.ai/search/fake-thread-1",
            "created_at": "2026-09-21T00:00:00Z",
            "turns": 1,
        }
        save_state(state)
        drv = ConsoleFakeDriver(share_tab=True,
                                url="https://www.perplexity.ai/search/fake-thread-1")
        drv.bubbles = ["旧问题\n13:39"]
        drv.prose = ["旧答案"]
        drv.studied = 1
        drv.misfire_until_reload = True
        res = _ask(drv, "文件里的数字", files=[str(f)])
        assert res["ok"]
        assert res["gates"]["submit"].get("recovered") == "reload"
        assert sum(1 for b in drv.bubbles if b.startswith("文件里的数字")) == 1
        assert res["answer"] == "答案 42。"

    def test_editor_state_desync_fails_loudly(self):
        # DOM shows the query but the editor state never commits (submit
        # button stays disabled even after a recovery reload): the fill gate
        # must fail, not submit.
        drv = ConsoleFakeDriver(force_btn_disabled=True)
        with pytest.raises(ConsoleError) as excinfo:
            _ask(drv, "q-状态脱钩")
        assert excinfo.value.gate == "fill"
        assert "btn='disabled'" in str(excinfo.value)
        assert drv.screenshots
        # the bounded recovery reload was attempted before giving up
        navs = [c for c in drv.calls if c[0] == "navigate"]
        assert any(c[2] is False for c in navs)

    def test_fill_desync_recovers_after_reload(self):
        state = load_state()
        state["threads"]["default"] = {
            "url": "https://www.perplexity.ai/search/fake-thread-1",
            "created_at": "2026-09-21T00:00:00Z",
            "turns": 1,
        }
        save_state(state)
        drv = ConsoleFakeDriver(share_tab=True,
                                url="https://www.perplexity.ai/search/fake-thread-1",
                                desync_until_reload=True)
        res = _ask(drv, "q-重载恢复")
        assert res["ok"]
        assert res["gates"]["fill"].get("recovered") == "reload"
        navs = [c for c in drv.calls if c[0] == "navigate"]
        assert any(c[1].endswith("/search/fake-thread-1") and c[2] is False
                   for c in navs)

    def test_submit_gate_failure_raises_with_evidence(self):
        drv = ConsoleFakeDriver(submit_disabled=True)
        with pytest.raises(ConsoleError) as excinfo:
            _ask(drv, "q-永不提交")
        assert excinfo.value.gate == "submit"
        assert excinfo.value.evidence
        assert excinfo.value.evidence.endswith(".png")
        assert drv.screenshots  # evidence screenshot was requested

    def test_continuation_reuses_thread_and_counts_turns(self):
        drv = ConsoleFakeDriver()
        _ask(drv, "第一个问题")
        creates_before = [c for c in drv.calls if c[0] == "navigate" and c[2] is True]
        res2 = _ask(drv, "第二个问题")
        assert res2["gates"]["attach"]["created"] is False
        assert res2["new_thread"] is False
        creates_after = [c for c in drv.calls if c[0] == "navigate" and c[2] is True]
        assert len(creates_after) == len(creates_before)  # no new tab for continuation
        state = load_state()
        assert state["threads"]["default"]["turns"] == 2

    def test_new_thread_flag_navigates_home_in_same_tab(self):
        drv = ConsoleFakeDriver(
            share_tab=True,
            url="https://www.perplexity.ai/search/old-thread",
        )
        drv.bubbles = ["旧问题\n13:39"]
        drv.prose = ["旧答案"]
        drv.studied = 1
        res = _ask(drv, "新任务问题", task="other", new_thread=True)
        navs = [c for c in drv.calls if c[0] == "navigate"]
        assert navs and navs[0][1] == BASE_URL and navs[0][2] is False
        assert res["gates"]["fill"]["ok"]
        assert res["url"].startswith("https://www.perplexity.ai/search/")
        state = load_state()
        assert state["threads"]["other"]["turns"] == 1
        assert state["threads"]["other"]["label"] == "新任务问题"

    def test_attach_error_when_extension_disconnected(self):
        class Disconnected(ConsoleFakeDriver):
            def list_tabs(self):
                return {"ok": False, "error": {"message": "no extension connected"}}

        with pytest.raises(ConsoleError) as excinfo:
            _ask(Disconnected(), "q")
        assert excinfo.value.gate == "attach"


class TestSelfcheckAndStatus:
    def test_selfcheck_reports_failure_dict(self):
        drv = ConsoleFakeDriver(submit_disabled=True)
        out = console_selfcheck(config=make_config(), driver=drv,
                                wait_budget=0.5, poll_interval=0.01,
                                submit_timeout=0.2, submit_recheck_timeout=0.1,
                                sleep=NOOP)
        assert out["ok"] is False
        assert out["gate"] == "submit"

    def test_status_readback(self):
        drv = ConsoleFakeDriver(share_tab=True)
        st = console_status(config=make_config(), driver=drv)
        assert st["live"]["tabs"] == 1
        assert st["live"]["on_thread"] is False
        assert st["pending"] is None
        assert console_threads()["threads"] == {}


class TestHelpers:
    def test_bubble_owns_requires_equality_or_prefix(self):
        assert _bubble_owns("那德国呢？\n13:20", "那德国呢？")
        assert _bubble_owns("用一句话回答：法国的首都是哪里？\n13:18",
                            "用一句话回答：法国的首都是哪里？")
        assert not _bubble_owns("13:18", "x")
        # extra text BEFORE the query is a polluted submission, not an owner
        assert not _bubble_owns("用一句话回答：法国的首都是哪里？ 13:18", "法国的首都是哪里")
        # a polluted bubble that merely CONTAINS the query must not pass
        assert not _bubble_owns("用一句话回答：法国的首都是哪里？\n用一句话回答：1+1 等于几？\n13:25",
                                "用一句话回答：1+1 等于几？")

    def test_url_matches(self):
        assert _url_matches("https://www.perplexity.ai/", BASE_URL)
        assert not _url_matches("https://www.perplexity.ai/search/x", BASE_URL)
        assert _url_matches("https://www.perplexity.ai/search/x",
                            "https://www.perplexity.ai/search/x")


class TestModels:
    def test_models_list_and_close(self):
        drv = ConsoleFakeDriver(share_tab=True)
        res = console_models(config=make_config(), driver=drv, sleep=NOOP)
        assert res["ok"]
        assert res["current"] == "Gemini 3.8 Flash"
        names = [m["name"] for m in res["models"]]
        assert "Claude Sonnet 5" in names and "GLM 5.3" in names
        checked = [m["name"] for m in res["models"] if m["checked"]]
        assert checked == ["Gemini 3.8 Flash"]
        assert res["menu_closed"] is True
        assert drv.model_menu_open is False

    def test_set_model_switch_and_partial_match(self):
        drv = ConsoleFakeDriver(share_tab=True)
        res = console_set_model("claude sonnet 5", config=make_config(),
                                driver=drv, sleep=NOOP)
        assert res["ok"] and res["to"] == "Claude Sonnet 5"
        assert res["from"] == "Gemini 3.8 Flash"
        assert drv.model == "Claude Sonnet 5"
        res2 = console_set_model("Gemini 3.8", config=make_config(),
                                 driver=drv, sleep=NOOP)
        assert res2["to"] == "Gemini 3.8 Flash"

    def test_set_model_not_found(self):
        drv = ConsoleFakeDriver(share_tab=True)
        with pytest.raises(ConsoleError) as excinfo:
            console_set_model("nope 9000", config=make_config(), driver=drv, sleep=NOOP)
        assert excinfo.value.gate == "model"
        assert "available:" in str(excinfo.value)
        assert drv.model_menu_open is False

    def test_set_model_submenu_rejected(self):
        drv = ConsoleFakeDriver(share_tab=True)
        with pytest.raises(ConsoleError) as excinfo:
            console_set_model("GPT-5.6 Sol", config=make_config(), driver=drv, sleep=NOOP)
        assert "submenu" in str(excinfo.value)
        assert drv.model == "Gemini 3.8 Flash"


class TestAskFiles:
    def test_ask_with_files_injects_verifies_and_sends(self, tmp_path):
        f = tmp_path / "console-attach-test.txt"
        f.write_text("42", encoding="utf-8")
        drv = ConsoleFakeDriver()
        res = _ask(drv, "文件里的数字是多少", files=[str(f)])
        assert res["ok"]
        assert res["attachments"] == ["console-attach-test.txt"]
        assert res["gates"]["files"]["ok"]
        assert res["gates"]["send"]["chips_cleared"] is True
        assert drv.attachments == []  # consumed by the submitted message

    def test_ask_file_missing_raises(self):
        drv = ConsoleFakeDriver()
        with pytest.raises(ConsoleError) as excinfo:
            _ask(drv, "q", files=["/definitely/not/here.txt"])
        assert excinfo.value.gate == "file"

    def test_ask_file_injection_failure_raises(self, tmp_path):
        f = tmp_path / "x.txt"
        f.write_text("1", encoding="utf-8")
        drv = ConsoleFakeDriver()
        drv.inject_fail = True
        with pytest.raises(ConsoleError) as excinfo:
            _ask(drv, "q", files=[str(f)])
        assert excinfo.value.gate == "file"

    def test_completion_waits_for_new_answer_not_stale(self):
        # The previous turn's answer must never satisfy the completion gate:
        # the new turn's prose has to appear first (slow-answer race observed
        # live 2026-09-21 when a file-bearing answer rendered late).
        state = load_state()
        state["threads"]["default"] = {
            "url": "https://www.perplexity.ai/search/fake-thread-1",
            "created_at": "2026-09-21T00:00:00Z",
            "turns": 1,
        }
        save_state(state)
        drv = ConsoleFakeDriver(share_tab=True,
                                url="https://www.perplexity.ai/search/fake-thread-1")
        drv.prose = ["旧答案"]
        drv.bubbles = ["旧问题\n13:39"]
        drv.studied = 1
        drv.answer_delay_samples = 6
        res = _ask(drv, "数字是多少")
        assert res["ok"]
        assert res["answer"] == "答案 42。"  # NOT the stale 旧答案
        assert res["gates"]["complete"]["new_seen"] is True


class TestGranularFlow:
    """Intent composition: fill → submit → wait → extract through the
    staged-turn ledger, one independent command per step."""

    def _seed_thread_state(self):
        state = load_state()
        state["threads"]["default"] = {
            "url": "https://www.perplexity.ai/search/fake-thread-1",
            "created_at": "2026-09-21T00:00:00Z",
            "turns": 0,
        }
        save_state(state)

    def test_fill_submit_wait_extract_cycle(self, tmp_path):
        f = tmp_path / "console-attach-test.txt"
        f.write_text("42", encoding="utf-8")
        self._seed_thread_state()
        drv = ConsoleFakeDriver(share_tab=True,
                                url="https://www.perplexity.ai/search/fake-thread-1")
        drv.bubbles = ["旧问题\n13:39"]
        drv.prose = ["旧答案"]
        drv.studied = 1

        r1 = console_fill("数字是多少", files=[str(f)],
                          config=make_config(), driver=drv, sleep=NOOP)
        assert r1["pending"]["query"] == "数字是多少"
        assert r1["pending"]["files"] == ["console-attach-test.txt"]
        assert load_state()["pending"]["status"] == "filled"

        r2 = console_submit(config=make_config(), driver=drv, sleep=NOOP,
                            submit_timeout=0.4, submit_recheck_timeout=0.1,
                            poll_interval=0.01)
        assert r2["gates"]["submit"]["ok"]
        assert load_state()["pending"]["status"] == "submitted"

        r3 = console_wait(config=make_config(), driver=drv, sleep=NOOP,
                          wait_budget=1.0, poll_interval=0.01)
        assert r3["gates"]["complete"]["ok"]

        r4 = console_extract(config=make_config(), driver=drv, sleep=NOOP)
        assert r4["answer"] == "答案 42。"
        st = load_state()
        assert not st.get("pending")
        assert st["threads"]["default"]["turns"] == 1

    def test_submit_without_pending_raises(self):
        drv = ConsoleFakeDriver(share_tab=True)
        with pytest.raises(ConsoleError) as ei:
            console_submit(config=make_config(), driver=drv, sleep=NOOP)
        assert ei.value.code == "pending.missing"

    def test_wait_before_submit_raises(self):
        state = load_state()
        state["pending"] = {
            "task": "default", "query": "q", "files": [], "file_paths": [],
            "url": "https://www.perplexity.ai/search/x",
            "base_bubbles": 0, "base_studied": 0, "base_prose_count": 0,
            "filled_at": _now_iso(), "status": "filled", "new_thread": False,
        }
        save_state(state)
        drv = ConsoleFakeDriver(share_tab=True, url="https://www.perplexity.ai/search/x")
        with pytest.raises(ConsoleError) as ei:
            console_wait(config=make_config(), driver=drv, sleep=NOOP)
        assert ei.value.code == "pending.not-submitted"

    def test_stale_pending_rejected(self):
        state = load_state()
        state["pending"] = {
            "task": "default", "query": "q", "files": [], "file_paths": [],
            "url": "https://www.perplexity.ai/search/x",
            "base_bubbles": 0, "base_studied": 0, "base_prose_count": 0,
            "filled_at": "2020-01-01T00:00:00Z", "status": "filled", "new_thread": False,
        }
        save_state(state)
        drv = ConsoleFakeDriver(share_tab=True, url="https://www.perplexity.ai/search/x")
        with pytest.raises(ConsoleError) as ei:
            console_submit(config=make_config(), driver=drv, sleep=NOOP)
        assert ei.value.code == "pending.stale"

    def test_submit_page_moved_rejected(self):
        state = load_state()
        state["pending"] = {
            "task": "default", "query": "q", "files": [], "file_paths": [],
            "url": "https://www.perplexity.ai/search/thread-A",
            "base_bubbles": 0, "base_studied": 0, "base_prose_count": 0,
            "filled_at": _now_iso(), "status": "filled", "new_thread": False,
        }
        save_state(state)
        drv = ConsoleFakeDriver(share_tab=True,
                                url="https://www.perplexity.ai/search/thread-B")
        with pytest.raises(ConsoleError) as ei:
            console_submit(config=make_config(), driver=drv, sleep=NOOP)
        assert ei.value.code == "pending.page-moved"

    def test_attach_idempotent_and_detach(self, tmp_path):
        f = tmp_path / "console-attach-test.txt"
        f.write_text("42", encoding="utf-8")
        drv = ConsoleFakeDriver(share_tab=True)
        r1 = console_attach([str(f)], config=make_config(), driver=drv, sleep=NOOP)
        assert "console-attach-test.txt" in r1["pending_files"]
        console_attach([str(f)], config=make_config(), driver=drv, sleep=NOOP)
        assert drv.attachments == ["console-attach-test.txt"]  # idempotent
        r3 = console_detach("console-attach-test.txt", config=make_config(),
                            driver=drv, sleep=NOOP)
        assert r3["chips"] == []
        assert drv.attachments == []
        assert not (load_state().get("pending") or {}).get("files")

    def test_send_composes_fill_and_submit(self):
        self._seed_thread_state()
        drv = ConsoleFakeDriver(share_tab=True,
                                url="https://www.perplexity.ai/search/fake-thread-1")
        r = console_send("q-send", config=make_config(), driver=drv, sleep=NOOP,
                         submit_timeout=0.4, submit_recheck_timeout=0.1,
                         poll_interval=0.01)
        assert r["ok"] and r["gates"]["submit"]["ok"]
        assert load_state()["pending"]["status"] == "submitted"


class TestJudgeIntegration:
    def test_ask_judge_disabled_by_default(self, monkeypatch):
        monkeypatch.delenv("PERPLEXITY_CONSOLE_JUDGE", raising=False)
        drv = ConsoleFakeDriver()
        res = _ask(drv, "q-judge-off")
        assert res["judge"] == {"enabled": False}

    def test_ask_judge_verdict_recorded(self, monkeypatch):
        from perplexity_toolkit import console_judge
        seen = {}

        def fake_judge(query, answer, *, client=None, flag=None):
            seen["query"], seen["answer"], seen["flag"] = query, answer, flag
            return {"enabled": True, "status": "ok",
                    "answers_question": 0.99, "complete": 0.98}

        monkeypatch.setattr(console_judge, "judge_extraction", fake_judge)
        drv = ConsoleFakeDriver()
        res = _ask(drv, "q-judge-on", judge=True)
        assert res["judge"]["status"] == "ok"
        assert seen["answer"] == "答案 42。"
        assert seen["flag"] is True

    def test_extract_judge_uses_pending_query(self, monkeypatch):
        from perplexity_toolkit import console_judge
        state = load_state()
        state["threads"]["default"] = {
            "url": "https://www.perplexity.ai/search/fake-thread-1",
            "created_at": "2026-09-21T00:00:00Z",
            "turns": 1,
        }
        state["pending"] = {
            "task": "default", "query": "文件里的数字", "new_thread": False,
            "files": [], "file_paths": [],
            "url": "https://www.perplexity.ai/search/fake-thread-1",
            "base_bubbles": 1, "base_studied": 1, "base_prose_count": 1,
            "filled_at": _now_iso(), "submitted_at": _now_iso(),
            "status": "completed",
        }
        save_state(state)
        seen = {}

        def fake_judge(query, answer, *, client=None, flag=None):
            seen["query"] = query
            return {"enabled": True, "status": "ok"}

        monkeypatch.setattr(console_judge, "judge_extraction", fake_judge)
        drv = ConsoleFakeDriver(share_tab=True,
                                url="https://www.perplexity.ai/search/fake-thread-1")
        drv.prose = ["答案 42。"]
        res = console_extract(config=make_config(), driver=drv, sleep=NOOP, judge=True)
        assert seen["query"] == "文件里的数字"
        assert res["judge"]["status"] == "ok"


class TestJevDirectedRecovery:
    """Jev decides among safe remedies; the code executes at most one attempt.

    Concept from browser-use/jev-ultrafast: Jev is the decision layer, code
    owns execution and safety bounds. Only fill/wait/extract are in scope —
    send paths stay advisory-only so an automatic retry can never send twice.
    """

    def test_fill_jev_recovery_after_builtin_reload_fails(self, monkeypatch):
        from perplexity_toolkit import console_judge

        state = load_state()
        state["threads"]["default"] = {
            "url": "https://www.perplexity.ai/search/fake-thread-1",
            "created_at": "2026-09-21T00:00:00Z",
            "turns": 1,
        }
        save_state(state)
        monkeypatch.setattr(
            console_judge, "route_hint",
            lambda gate, code, message, *, client=None, flag=None: {
                "enabled": True, "status": "ok", "route": "reload-and-retry",
                "confidence": 0.72})
        # editor desync that survives the BUILT-IN single-reload recovery:
        # only a SECOND navigation heals it — the Jev-directed one.
        drv = ConsoleFakeDriver(
            share_tab=True, url="https://www.perplexity.ai/search/fake-thread-1",
            desync_until_reload=True, desync_recover_after=2)
        res = console_fill("q-jev", config=make_config(), driver=drv, sleep=NOOP,
                           judge=True)
        assert res["ok"]
        jr = res["gates"]["jev_recovery"]
        assert jr["route"] == "reload-and-retry"
        assert jr["applied"] is True and jr["attempts"] == 1
        navs = [c for c in drv.calls if c[0] == "navigate" and c[2] is False]
        assert len(navs) >= 2  # built-in reload + Jev-directed reload

    def test_fill_jev_recovery_escalate_raises_original(self, monkeypatch):
        from perplexity_toolkit import console_judge

        monkeypatch.setattr(
            console_judge, "route_hint",
            lambda gate, code, message, *, client=None, flag=None: {
                "enabled": True, "status": "ok", "route": "escalate",
                "confidence": 0.9})
        drv = ConsoleFakeDriver(force_btn_disabled=True)
        with pytest.raises(ConsoleError) as excinfo:
            console_fill("q-jev-escalate", config=make_config(), driver=drv,
                         sleep=NOOP, judge=True)
        assert excinfo.value.gate == "fill"
        # escalate -> nothing executed, no recovery metadata attached
        assert "jev_recovery" not in (excinfo.value.gates or {})

    def test_send_paths_never_ask_jev_for_recovery(self, monkeypatch):
        from perplexity_toolkit import console_judge

        calls = []

        def spy(gate, code, message, *, client=None, flag=None):
            calls.append(gate)
            return {"enabled": True, "status": "ok", "route": "retry-step",
                    "confidence": 0.5}

        monkeypatch.setattr(console_judge, "route_hint", spy)
        drv = ConsoleFakeDriver(force_btn_disabled=True)
        with pytest.raises(ConsoleError):
            console_submit(config=make_config(), driver=drv, sleep=NOOP)
        assert calls == []  # send paths stay advisory-only


IDLE_ICON = "#pplx-icon-arrow-up"   # live-verified idle action icon (2026-09-23)
BUSY_ICON = "#pplx-icon-stop"       # any non-idle icon ⇒ busy (inverted guard)


class TestAnswerSettled:
    """v2.2 predicate (live-verified 2026-09-23): the action button's STATE
    cannot discriminate busy/done (it stays 提交+disabled once the composer
    clears); its inner `svg use` ICON morphs instead. The 已研究 pill only
    appears on Pro-Search turns and the whole-page `generating` regex is
    pinned true by the model badge — both advisory only."""

    def test_stop_visible_blocks_even_when_length_stable(self):
        info = {"stop_button": True, "action_icon": IDLE_ICON, "lastProseLen": 120}
        assert console._answer_settled(info, 120, 5, new_seen=True) == (False, "")

    def test_non_idle_action_icon_blocks(self):
        info = {"stop_button": False, "action_icon": BUSY_ICON, "lastProseLen": 120}
        assert console._answer_settled(info, 120, 5, new_seen=True) == (False, "")

    def test_idle_icon_stable_and_new_answer_settles(self):
        info = {"stop_button": False, "action_icon": IDLE_ICON, "lastProseLen": 120}
        assert console._answer_settled(info, 120, 2, new_seen=True) == (True, "icon+stable")

    def test_blank_icon_downgrades_to_length_only(self):
        info = {"stop_button": False, "action_icon": "", "lastProseLen": 120}
        assert console._answer_settled(info, 120, 2, new_seen=True) == (True, "length-only")

    def test_missing_action_icon_key_downgrades_to_length_only(self):
        # 兼容无 action_icon 的旧探针/假驱动：信号降级，不误判忙
        info = {"stop_button": False, "lastProseLen": 120}
        assert console._answer_settled(info, 120, 2, new_seen=True) == (True, "length-only")

    def test_without_new_answer_never_settles(self):
        info = {"stop_button": False, "action_icon": IDLE_ICON, "lastProseLen": 120}
        assert console._answer_settled(info, 120, 5, new_seen=False) == (False, "")

    def test_unstable_length_never_settles(self):
        info = {"stop_button": False, "action_icon": IDLE_ICON, "lastProseLen": 120}
        assert console._answer_settled(info, 120, 1, new_seen=True) == (False, "")

    def test_zero_length_never_settles(self):
        info = {"stop_button": False, "action_icon": IDLE_ICON, "lastProseLen": 0}
        assert console._answer_settled(info, 0, 2, new_seen=True) == (False, "")

    def test_generating_text_is_advisory_only(self):
        info = {"stop_button": False, "action_icon": IDLE_ICON, "lastProseLen": 120,
                "generating": True}
        assert console._answer_settled(info, 120, 2, new_seen=True) == (True, "icon+stable")


class TestInfoProbe:
    def test_probe_bilingual_submit_and_visible_stop(self):
        assert 'aria-label="搜索"' in console._JS_INFO
        assert 'aria-label="Submit"' in console._JS_INFO
        assert 'checkVisibility' in console._JS_INFO
        assert 'stop_button' in console._JS_INFO
        assert 'lastProseLen' in console._JS_INFO and 'studied' in console._JS_INFO

    def test_probe_carries_action_icon(self):
        # 真机实测：按钮状态分不了忙/闲，svg use 图标 ID 可以（T4 新增字段）
        assert 'action_icon' in console._JS_INFO
        assert 'svg use' in console._JS_INFO
        assert 'xlink:href' in console._JS_INFO

    def test_fake_driver_info_carries_new_fields(self):
        d = ConsoleFakeDriver(answer="answer", share_tab=True)
        console_fill("hi", config=make_config(), driver=d, sleep=NOOP)
        console_submit(config=make_config(), driver=d, sleep=NOOP)
        info = console._info(d)
        assert "stop_button" in info
        assert info["submit_button"] in ("enabled", "disabled", "missing")
        assert info["action_icon"] == IDLE_ICON

    def test_info_passes_mutating_false(self):
        """_info calls _js with mutating=False so the WebBridge can retry
        the probe on timeout (read-only)."""
        class SpyDriver:
            captured = None
            def evaluate(self, code, **kwargs):
                self.__class__.captured = kwargs.get("mutating")
                return {}
        from perplexity_toolkit.console import _info
        _info(SpyDriver())
        assert SpyDriver.captured is False


def _info_item(*, length=0, prose=0, stop=False, icon=IDLE_ICON,
               generating=False, studied=0):
    """One scripted _JS_INFO sample for TestGateCompleteV2."""
    return {
        "url": BASE_URL, "title": "Perplexity", "composer": "",
        "submit_button": "disabled", "action_icon": icon,
        "stop_button": stop, "model": "Gemini 3.8 Flash",
        "bubbles": 1, "lastBubble": "hi\n13:40", "studied": studied,
        "generating": generating, "proseCount": prose, "lastProseLen": length,
    }


class ScriptedInfoDriver(ConsoleFakeDriver):
    """evaluate(info) yields a scripted sequence; the LAST item repeats
    forever (never StopIteration)."""

    def __init__(self, sequence, **kw):
        super().__init__(share_tab=True, **kw)
        assert sequence, "scripted driver needs at least one info sample"
        self._script = [dict(x) for x in sequence]
        self._script_i = 0

    def evaluate(self, code, **kwargs):
        if "user-bubble" in code:
            item = self._script[min(self._script_i, len(self._script) - 1)]
            self._script_i += 1
            return dict(item)
        return super().evaluate(code)


class EmptyProbeDriver(ConsoleFakeDriver):
    """The bridge answers every info probe with nothing."""

    def evaluate(self, code, **kwargs):
        if "user-bubble" in code:
            return {}
        return super().evaluate(code)


class TestGateCompleteV2:
    """_gate_complete v2: adaptive deadline + contradiction flag +
    empty-probe fail-fast, decided by the v2.2 icon predicate."""

    @staticmethod
    def _gate(drv, **kw):
        kw.setdefault("base_studied", 0)
        kw.setdefault("base_prose_count", 1)
        kw.setdefault("wait_budget", 5.0)
        kw.setdefault("poll", 0.01)
        kw.setdefault("sleep", NOOP)
        return console._gate_complete(drv, kw.pop("base_studied"), **kw)

    @staticmethod
    def _fake_time(step):
        """Injectable clock: sleep advances time deterministically."""
        state = {"t": 0.0}

        def mono():
            return state["t"]

        def slp(_seconds):
            state["t"] += step

        return mono, slp

    def test_streaming_then_done_releases_with_icon_signal(self):
        seq = [_info_item(length=120, prose=1, stop=True) for _ in range(3)]
        seq += [_info_item(length=200, prose=2) for _ in range(3)]
        res = self._gate(ScriptedInfoDriver(seq))
        assert res["ok"] is True
        assert res["signal"] == "icon+stable"
        assert res["contradiction"] is False

    def test_composer_clears_fill_submit_then_gate_releases(self):
        # composer_clears ⇒ 提交后按钮回 disabled 稳态；icon 是放行依据
        d = ConsoleFakeDriver(composer_clears=True, share_tab=True)
        console_fill("hi", config=make_config(), driver=d, sleep=NOOP)
        console_submit(config=make_config(), driver=d, sleep=NOOP)
        res = console._gate_complete(d, 0, base_prose_count=0,
                                     wait_budget=5.0, poll=0.01, sleep=NOOP)
        assert res["ok"] is True
        assert res["signal"] == "icon+stable"

    def test_generating_noise_does_not_block_release(self):
        # generating 被 model 徽标钉死恒真 ⇒ 不参与放行判定
        seq = [_info_item(length=120, prose=1, stop=True, generating=True)
               for _ in range(3)]
        seq += [_info_item(length=200, prose=2, generating=True) for _ in range(3)]
        res = self._gate(ScriptedInfoDriver(seq))
        assert res["ok"] is True
        assert res["signal"] == "icon+stable"
        assert res["generating"] is True

    def test_empty_probe_fails_fast(self):
        with pytest.raises(ConsoleError) as exc:
            self._gate(EmptyProbeDriver(), wait_budget=1.0)
        assert exc.value.code == "bridge.probe-failed"

    def test_growing_answer_extends_deadline(self):
        mono, slp = self._fake_time(0.5)
        seq = [_info_item(length=100 + i, prose=2, stop=True) for i in range(40)]
        seq.append(_info_item(length=400, prose=2))
        res = self._gate(ScriptedInfoDriver(seq), base_len=50, wait_budget=6.0,
                         poll=0.5, sleep=slp, monotonic=mono)
        assert res["ok"] is True
        assert res["deadline_extensions"] >= 1
        assert res["elapsed_s"] > 6

    def test_busy_but_frozen_content_flags_contradiction(self):
        # 忙信号在但内容 25 拍不动 ⇒ 证据矛盾只标记，不阻断最终放行
        seq = [_info_item(length=100, prose=1, icon=BUSY_ICON) for _ in range(25)]
        seq += [_info_item(length=200, prose=2) for _ in range(3)]
        res = self._gate(ScriptedInfoDriver(seq), poll=0.02, base_len=50)
        assert res["ok"] is True
        assert res["contradiction"] is True

    def test_stuck_busy_times_out_fail_closed(self):
        mono, slp = self._fake_time(1.0)
        drv = ScriptedInfoDriver([_info_item(length=300, prose=1, stop=True)])
        with pytest.raises(ConsoleError) as exc:
            self._gate(drv, base_len=100, wait_budget=5.0, poll=1.0,
                       sleep=slp, monotonic=mono)
        assert exc.value.code == "complete.timeout"
        detail = exc.value.gates["complete"]
        assert detail["escalation"] == "human-review"

    def test_never_new_answer_never_releases(self):
        # 长度从未超 base_len、proseCount 从未超 base_prose_count ⇒ 即使
        # 稳定也不放行（stale answer 门槛），最终 fail-closed 超时
        mono, slp = self._fake_time(1.0)
        drv = ScriptedInfoDriver([_info_item(length=250, prose=1)])
        with pytest.raises(ConsoleError) as exc:
            self._gate(drv, base_len=250, base_prose_count=1,
                       wait_budget=5.0, poll=1.0, sleep=slp, monotonic=mono)
        assert exc.value.code == "complete.timeout"
        assert exc.value.gates["complete"]["new_seen"] is False


# ──────────────────────────────────────────────────────────────
# T5: scroll-to-bottom before extraction (2026-09-23)
# ──────────────────────────────────────────────────────────────

class ScrollScriptedDriver(ConsoleFakeDriver):
    """evaluate(_JS_SCROLL) yields scripted samples; the LAST repeats forever."""

    def __init__(self, scroll_seq, **kw):
        super().__init__(share_tab=True, **kw)
        assert scroll_seq, "scroll script needs at least one sample"
        self._scroll = [dict(x) for x in scroll_seq]
        self._scroll_i = 0

    def evaluate(self, code, **kwargs):
        if "scrollTop" in code:
            item = self._scroll[min(self._scroll_i, len(self._scroll) - 1)]
            self._scroll_i += 1
            return dict(item)
        return super().evaluate(code)


def _scroll_item(bottom=True, height=1000, scrolled=True,
                 container="DIV.scrollable-container"):
    return {"bottom": bottom, "height": height, "scrolled": scrolled,
            "container": container}


class TestScrollToBottom:
    """滚动到底探针：真机滚动容器是 div.scrollable-container.overflow-auto
    （main 内 prose 的可滚动祖先，2026-09-23 DOM 实测）。"""

    def test_probe_discovers_scrollable_ancestor(self):
        # 发现算法：从最后一个 div.prose 向上找 overflow-y 可滚动祖先，
        # 兜底 document.scrollingElement
        assert "div.prose" in console._JS_SCROLL
        assert "overflowY" in console._JS_SCROLL
        assert "scrollHeight" in console._JS_SCROLL
        assert "scrollingElement" in console._JS_SCROLL

    def test_container_found_and_two_equal_reads_settle(self):
        drv = ScrollScriptedDriver([_scroll_item(),
                                    _scroll_item(scrolled=False)])
        res = console._scroll_to_bottom(drv, prose_len=lambda: 42, sleep=NOOP)
        assert res["settled"] is True
        assert res["rounds"] == 2
        assert res["container"] == "DIV.scrollable-container"
        assert res["prose"] == 42
        assert "warning" not in res

    def test_unsettled_carries_warning_code(self):
        drv = ScrollScriptedDriver([_scroll_item(bottom=False)])
        res = console._scroll_to_bottom(drv, prose_len=lambda: 42,
                                        sleep=NOOP, max_rounds=3)
        assert res["settled"] is False
        assert res["warning"] == "complete.scroll-unsettled"

    def test_empty_prose_never_settles(self):
        # 等长但长度为 0 不算 settle（lazy 渲染还没出内容）
        drv = ScrollScriptedDriver([_scroll_item()])
        res = console._scroll_to_bottom(drv, prose_len=lambda: 0,
                                        sleep=NOOP, max_rounds=3)
        assert res["settled"] is False
        assert res["warning"] == "complete.scroll-unsettled"

    def test_non_dict_probe_result_treated_as_empty(self):
        class IntDriver(ConsoleFakeDriver):
            def evaluate(self, code, **kwargs):
                if "scrollTop" in code:
                    return 7
                return super().evaluate(code)

        drv = IntDriver(share_tab=True)
        res = console._scroll_to_bottom(drv, prose_len=lambda: 42,
                                        sleep=NOOP, max_rounds=2)
        assert isinstance(res, dict)
        assert res["settled"] is False

    def test_extract_result_carries_scroll_gate(self):
        drv = ScrollScriptedDriver([_scroll_item()], answer="答案 42。")
        console_fill("hi", config=make_config(), driver=drv, sleep=NOOP)
        console_submit(config=make_config(), driver=drv, sleep=NOOP)
        res = console_extract(config=make_config(), driver=drv, sleep=NOOP)
        assert res["gates"]["scroll"]["settled"] is True
        assert res["gates"]["scroll"]["container"] == "DIV.scrollable-container"


# ──────────────────────────────────────────────────────────────
# T6: truncation-risk flag (advisory by default; strict is a hard gate)
# ──────────────────────────────────────────────────────────────

class TestTruncationRisk:
    def test_busy_without_terminator_is_risk(self):
        assert console._truncation_risk("答案写到一半",
                                        still_generating=True) is True

    def test_terminator_tail_is_not_risk(self):
        assert console._truncation_risk("答案 42。",
                                        still_generating=True) is False

    def test_idle_is_never_risk(self):
        # 无论尾部有没有终止符，不忙就不算截断
        assert console._truncation_risk("答案写到一半",
                                        still_generating=False) is False
        assert console._truncation_risk("答案 42。",
                                        still_generating=False) is False

    def test_empty_text_is_not_risk(self):
        assert console._truncation_risk("", still_generating=True) is False

    def test_field_names_and_values(self):
        risky = console._truncation_risk_fields("一半", still_generating=True)
        assert risky == {"answer_terminated": False, "truncation_risk": True}
        done = console._truncation_risk_fields("完。", still_generating=True)
        assert done == {"answer_terminated": True, "truncation_risk": False}

    @staticmethod
    def _busy_extract(answer):
        """fill → submit → extract against a still-generating fake page."""
        drv = ConsoleFakeDriver(share_tab=True, answer=answer, busy=True)
        console_fill("hi", config=make_config(), driver=drv, sleep=NOOP)
        console_submit(config=make_config(), driver=drv, sleep=NOOP)
        return console_extract(config=make_config(), driver=drv, sleep=NOOP), drv

    @staticmethod
    def _idle_extract(answer):
        drv = ConsoleFakeDriver(share_tab=True, answer=answer)
        console_fill("hi", config=make_config(), driver=drv, sleep=NOOP)
        console_submit(config=make_config(), driver=drv, sleep=NOOP)
        return console_extract(config=make_config(), driver=drv, sleep=NOOP), drv

    def test_extract_flags_truncation_while_busy(self):
        res, _ = self._busy_extract("答案写到一半")
        assert res["truncation_risk"] is True
        assert res["answer_terminated"] is False

    def test_extract_terminated_busy_answer_not_flagged(self):
        res, _ = self._busy_extract("答案 42。")
        assert res["truncation_risk"] is False
        assert res["answer_terminated"] is True

    def test_extract_idle_answer_not_flagged(self):
        res, _ = self._idle_extract("答案写到一半")
        assert res["truncation_risk"] is False
        assert res["answer_terminated"] is True

    def test_default_extract_only_flags_no_raise(self):
        res, _ = self._busy_extract("答案写到一半")
        assert res["ok"] is True
        assert res["truncation_risk"] is True

    def test_strict_extract_raises_truncation_error(self):
        drv = ConsoleFakeDriver(share_tab=True, answer="答案写到一半",
                                busy=True)
        console_fill("hi", config=make_config(), driver=drv, sleep=NOOP)
        console_submit(config=make_config(), driver=drv, sleep=NOOP)
        with pytest.raises(ConsoleError) as exc:
            console_extract(config=make_config(), driver=drv, sleep=NOOP,
                            strict=True)
        assert exc.value.code == "extract.truncation-risk"

    def test_strict_extract_passes_when_answer_terminated(self):
        drv = ConsoleFakeDriver(share_tab=True, answer="答案 42。")
        console_fill("hi", config=make_config(), driver=drv, sleep=NOOP)
        console_submit(config=make_config(), driver=drv, sleep=NOOP)
        res = console_extract(config=make_config(), driver=drv, sleep=NOOP,
                              strict=True)
        assert res["ok"] is True

    def test_log_run_records_truncation_risk(self, tmp_path, monkeypatch):
        monkeypatch.setattr(console, "console_home", lambda: tmp_path)
        console._log_run("extract", ok=True, url="u", truncation_risk=True)
        rec = json.loads((tmp_path / "runs.jsonl").read_text(
            encoding="utf-8").strip().splitlines()[-1])
        assert rec["truncation_risk"] is True

    def test_log_run_omits_absent_truncation_risk(self, tmp_path, monkeypatch):
        monkeypatch.setattr(console, "console_home", lambda: tmp_path)
        console._log_run("extract", ok=True, url="u")
        rec = json.loads((tmp_path / "runs.jsonl").read_text(
            encoding="utf-8").strip().splitlines()[-1])
        assert "truncation_risk" not in rec

    def test_extract_success_path_logs_truncation_risk(self, tmp_path, monkeypatch):
        monkeypatch.setattr(console, "console_home", lambda: tmp_path)
        drv = ConsoleFakeDriver(share_tab=True, answer="答案写到一半",
                                busy=True)
        console_fill("hi", config=make_config(), driver=drv, sleep=NOOP)
        console_submit(config=make_config(), driver=drv, sleep=NOOP)
        console_extract(config=make_config(), driver=drv, sleep=NOOP)
        lines = [json.loads(x) for x in
                 (tmp_path / "runs.jsonl").read_text(encoding="utf-8").splitlines()]
        rec = [x for x in lines if x.get("op") == "extract"][-1]
        assert rec["truncation_risk"] is True


# ──────────────────────────────────────────────────────────────
# T6 CLI: extract warnings + --strict wiring
# ──────────────────────────────────────────────────────────────

class TestCliExtractWarnings:
    """`_t_extract` must surface the scroll/truncation verdicts as text."""

    @staticmethod
    def _payload(**over):
        p = {"ok": True, "answer": "半截答案", "url": "u", "model": "m",
             "sources": [], "gates": {}, "judge": {"enabled": False}}
        p.update(over)
        return p

    @staticmethod
    def _run(monkeypatch, capsys, payload, extra=(), expect_rc=0):
        captured = {}

        def fake(**kw):
            captured.update(kw)
            return payload

        monkeypatch.setattr(console, "console_extract", fake)
        args = build_parser().parse_args(["console", "extract", *extra])
        rc = cmd_console(args)
        return rc, capsys.readouterr().out, captured

    def test_scroll_unsettled_warning_printed(self, monkeypatch, capsys):
        payload = self._payload(
            gates={"scroll": {"settled": False,
                              "warning": "complete.scroll-unsettled"}})
        rc, out, _ = self._run(monkeypatch, capsys, payload)
        assert rc == 0
        assert "⚠️ scroll unsettled（懒渲染可能未完成，extract --peek 复核）" in out

    def test_truncation_warning_printed(self, monkeypatch, capsys):
        payload = self._payload(truncation_risk=True)
        rc, out, _ = self._run(monkeypatch, capsys, payload)
        assert rc == 0
        assert "⚠️ 可能截断（尾部无终止符且仍在生成）" in out

    def test_no_warnings_when_clean(self, monkeypatch, capsys):
        payload = self._payload(gates={"scroll": {"settled": True}},
                                truncation_risk=False)
        rc, out, _ = self._run(monkeypatch, capsys, payload)
        assert rc == 0
        assert "⚠️" not in out

    def test_strict_flag_forwarded(self, monkeypatch, capsys):
        payload = self._payload(gates={"scroll": {"settled": True}},
                                truncation_risk=False)
        rc, out, captured = self._run(monkeypatch, capsys, payload,
                                      extra=("--strict",))
        assert rc == 0
        assert captured.get("strict") is True

    def test_strict_defaults_to_false(self, monkeypatch, capsys):
        payload = self._payload(gates={"scroll": {"settled": True}},
                                truncation_risk=False)
        rc, out, captured = self._run(monkeypatch, capsys, payload)
        assert rc == 0
        assert captured.get("strict") is False

    def test_strict_truncation_error_fails_cli(self, monkeypatch, capsys):
        from perplexity_toolkit import console_judge
        monkeypatch.setattr(console_judge, "route_hint",
                            lambda *a, **k: {"status": "skipped"})

        def raise_strict(**kw):
            raise ConsoleError("extract", "答案疑似截断（strict 模式）",
                               code="extract.truncation-risk")

        monkeypatch.setattr(console, "console_extract", raise_strict)
        args = build_parser().parse_args(["console", "extract", "--strict"])
        rc = cmd_console(args)
        out = capsys.readouterr().out
        assert rc == 1
        assert "extract.truncation-risk" in out


# ──────────────────────────────────────────────────────────────
# T7: extract --peek/--again — read-only re-read (no consume, no reload)
# ──────────────────────────────────────────────────────────────

class TestExtractPeekAgain:
    """`peek` re-reads the staged turn without consuming it; `again` re-reads
    without a staged turn at all. Both forbid the reload-and-retry remedy
    (a reload would re-stream the page under a read-only call)."""

    def test_peek_reads_but_does_not_consume(self):
        drv = ConsoleFakeDriver(share_tab=True)
        console_fill("hi", config=make_config(), driver=drv, sleep=NOOP)
        console_submit(config=make_config(), driver=drv, sleep=NOOP)
        state = load_state()
        assert state["pending"] is not None
        state["threads"]["default"] = {
            "url": "https://www.perplexity.ai/search/fake-thread-1",
            "created_at": "2026-09-21T00:00:00Z", "turns": 3,
        }
        save_state(state)
        drv.prose = ["答案 42。"]
        out = console._extract_step(drv, state, make_config(), sleep=NOOP,
                                    pending=state["pending"], consume=False)
        assert out["ok"] is True
        assert out["answer"] == "答案 42。"
        assert out["consumed"] is False
        # disk-level: staged turn survives, turn count unchanged
        st = load_state()
        assert st["pending"] is not None
        assert st["threads"]["default"]["turns"] == 3

    def test_again_reads_without_pending(self):
        assert load_state()["pending"] is None
        drv = ConsoleFakeDriver(share_tab=True)
        drv.prose = ["答案 42。"]
        res = console_extract(config=make_config(), driver=drv, sleep=NOOP,
                              again=True)
        assert res["ok"] is True
        assert res["answer"] == "答案 42。"
        assert res["consumed"] is False
        assert load_state()["pending"] is None

    def test_peek_again_disable_reload(self, monkeypatch):
        from perplexity_toolkit import console_judge
        monkeypatch.setattr(
            console_judge, "route_hint",
            lambda gate, code, message, *, client=None, flag=None: {
                "enabled": True, "status": "ok", "route": "reload-and-retry",
                "confidence": 0.8})
        sentinel = []
        monkeypatch.setattr(console, "_reload_console_tab",
                            lambda *a, **k: sentinel.append(True))

        class FlakyProseDriver(ConsoleFakeDriver):
            """First prose probe finds nothing (recoverable extract.empty)."""
            def __init__(self, **kw):
                super().__init__(**kw)
                self.prose_fails = 1

            def evaluate(self, code, **kwargs):
                if self.prose_fails > 0 and "cloneNode" in code:
                    self.prose_fails -= 1
                    return {"found": False}
                return super().evaluate(code)

        # default path: the ladder is allowed to reload (sentinel touched)
        drv = FlakyProseDriver(share_tab=True)
        drv.prose = ["答案 42。"]
        res = console_extract(config=make_config(), driver=drv, sleep=NOOP,
                              judge=True)
        assert res["ok"] is True
        assert sentinel, "default recovery path should attempt reload"
        assert res["gates"]["jev_recovery"]["route"] == "reload-and-retry"

        # peek: reload forbidden — original error stands, sentinel untouched
        sentinel.clear()
        drv2 = FlakyProseDriver(share_tab=True)
        drv2.prose = ["答案 42。"]
        with pytest.raises(ConsoleError) as exc:
            console_extract(config=make_config(), driver=drv2, sleep=NOOP,
                            judge=True, peek=True)
        assert exc.value.code == "extract.empty"
        assert sentinel == []

        # again: same restriction
        sentinel.clear()
        drv3 = FlakyProseDriver(share_tab=True)
        drv3.prose = ["答案 42。"]
        with pytest.raises(ConsoleError) as exc3:
            console_extract(config=make_config(), driver=drv3, sleep=NOOP,
                            judge=True, again=True)
        assert exc3.value.code == "extract.empty"
        assert sentinel == []

    def test_peek_again_flags_conflict(self):
        with pytest.raises(ConsoleError) as exc:
            console_extract(config=make_config(), driver=ConsoleFakeDriver(),
                            sleep=NOOP, peek=True, again=True)
        assert exc.value.code == "extract.flag-conflict"


class TestCliExtractPeekAgain:
    """`--peek`/`--again` are forwarded and the consumed verdict prints."""

    @staticmethod
    def _run(monkeypatch, capsys, extra):
        captured = {}

        def fake(**kw):
            captured.update(kw)
            return {"ok": True, "answer": "答案 42。", "url": "u",
                    "model": "m", "sources": [], "gates": {},
                    "judge": {"enabled": False}, "consumed": False}

        monkeypatch.setattr(console, "console_extract", fake)
        args = build_parser().parse_args(["console", "extract", *extra])
        rc = cmd_console(args)
        return rc, capsys.readouterr().out, captured

    def test_peek_flag_forwarded_and_shows_consumed(self, monkeypatch, capsys):
        rc, out, captured = self._run(monkeypatch, capsys, ("--peek",))
        assert rc == 0
        assert captured.get("peek") is True
        assert "consumed: false" in out

    def test_again_flag_forwarded_and_not_consuming(self, monkeypatch, capsys):
        rc, out, captured = self._run(monkeypatch, capsys, ("--again",))
        assert rc == 0
        assert captured.get("again") is True
        assert "consumed: false" in out


def _last_run_record(home):
    """True-disk read of the LAST runs.jsonl line ({} when absent)."""
    path = home / "runs.jsonl"
    if not path.exists():
        return {}
    lines = path.read_text(encoding="utf-8").strip().splitlines()
    return json.loads(lines[-1]) if lines else {}


class TestFailureLoggingAndReattach:
    """T8: every failing op leaves `ok=False` + error code in runs.jsonl;
    a page-moved / bridge-lost submit re-attaches ONCE and never re-sends."""

    def test_wait_timeout_is_logged_with_error_code(self, tmp_console_home,
                                                     monkeypatch):
        from perplexity_toolkit import console_judge

        # deterministic offline route: no retry, the original error stands
        monkeypatch.setattr(
            console_judge, "route_hint",
            lambda gate, code, message, *, client=None, flag=None: {
                "enabled": True, "status": "ok", "route": "escalate",
                "confidence": 0.9})
        drv = ConsoleFakeDriver(share_tab=True)
        console_fill("hi", config=make_config(), driver=drv, sleep=NOOP)
        console_submit(config=make_config(), driver=drv, sleep=NOOP)

        def timeout(*args, **kwargs):
            raise ConsoleError("complete", "settle budget exhausted",
                               code="complete.timeout")

        monkeypatch.setattr(console, "_gate_complete", timeout)
        with pytest.raises(ConsoleError) as exc:
            console_wait(config=make_config(), driver=drv, sleep=NOOP)
        assert exc.value.code == "complete.timeout"
        rec = _last_run_record(tmp_console_home)
        assert rec.get("ok") is False
        assert rec.get("error") == "complete.timeout"
        assert rec.get("op") == "wait"

    def test_send_path_failure_is_logged_too(self, tmp_console_home,
                                              monkeypatch):
        from perplexity_toolkit import console_judge

        # (a) console_submit failure leaves an ok=False + code record
        drv = ConsoleFakeDriver(share_tab=True)
        console_fill("hi", config=make_config(), driver=drv, sleep=NOOP)
        drv.force_btn_disabled = True   # editor never commits → gate fails
        with pytest.raises(ConsoleError) as exc:
            console_submit(config=make_config(), driver=drv, sleep=NOOP,
                           submit_timeout=0.2, submit_recheck_timeout=0.05)
        assert exc.value.code
        rec = _last_run_record(tmp_console_home)
        assert rec.get("ok") is False
        assert rec.get("error") == exc.value.code
        assert rec.get("op") == "submit"

        # (b) the jev-recovery exc2 path (retried run failed again) too
        monkeypatch.setattr(
            console_judge, "route_hint",
            lambda gate, code, message, *, client=None, flag=None: {
                "enabled": True, "status": "ok", "route": "retry-step",
                "confidence": 0.8})

        class ProseFailsTwiceDriver(ConsoleFakeDriver):
            """First TWO prose probes find nothing → run + jev retry both fail."""

            def __init__(self, **kw):
                super().__init__(share_tab=True, **kw)
                self.prose_fails = 2

            def evaluate(self, code, **kwargs):
                if self.prose_fails > 0 and "cloneNode" in code:
                    self.prose_fails -= 1
                    return {"found": False}
                return super().evaluate(code)

        drv2 = ProseFailsTwiceDriver()
        drv2.prose = ["答案 42。"]
        with pytest.raises(ConsoleError) as exc2:
            console_extract(config=make_config(), driver=drv2, sleep=NOOP,
                            judge=True)
        assert exc2.value.code == "extract.empty"
        rec2 = _last_run_record(tmp_console_home)
        assert rec2.get("ok") is False
        assert rec2.get("error") == "extract.empty"
        assert rec2.get("op") == "extract"

    def test_page_moved_reattach_retries_verify_never_resubmit(self,
                                                               monkeypatch):
        drv = ConsoleFakeDriver(share_tab=True)
        console_fill("hi", config=make_config(), driver=drv, sleep=NOOP)
        staged_url = load_state()["pending"]["url"]
        # the tab drifts away from the staged thread after fill
        drv.url = "https://www.perplexity.ai/search/someone-elses-thread"
        drv.tab_url = drv.url

        opened = []

        def fake_open(target, **kwargs):
            opened.append(target)
            drv.url = staged_url          # re-attach lands back on the thread
            drv.tab_url = staged_url
            return {"ok": True, "target": target}

        monkeypatch.setattr(console, "console_open", fake_open)

        gate_calls = []
        real_gate_submit = console._gate_submit

        def counting_gate(*args, **kwargs):
            gate_calls.append(1)
            return real_gate_submit(*args, **kwargs)

        monkeypatch.setattr(console, "_gate_submit", counting_gate)

        res = console_submit(config=make_config(), driver=drv, sleep=NOOP,
                             submit_timeout=0.4, submit_recheck_timeout=0.1)
        assert res["ok"] is True
        assert res["reattached"] is True
        assert opened == ["default"]      # bounded: exactly one re-attach
        assert len(gate_calls) == 1       # submit executor ran exactly once
        assert len(drv.bubbles) == 1      # double-send detector: one turn only

    def test_reattach_disabled_behaves_as_before(self, monkeypatch):
        drv = ConsoleFakeDriver(share_tab=True)
        console_fill("hi", config=make_config(), driver=drv, sleep=NOOP)
        drv.url = "https://www.perplexity.ai/search/someone-elses-thread"
        drv.tab_url = drv.url

        opened = []
        monkeypatch.setattr(
            console, "console_open",
            lambda target, **kw: opened.append(target) or {"ok": True})
        with pytest.raises(ConsoleError) as exc:
            console_submit(config=make_config(), driver=drv, sleep=NOOP,
                           allow_reattach=False)
        assert exc.value.code == "pending.page-moved"
        assert opened == []               # no re-attach attempted
        assert drv.bubbles == []          # nothing was sent
