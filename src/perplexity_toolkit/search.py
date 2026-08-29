"""Core Perplexity search functions with anti-detection.

Each function returns a SearchResult dict with:
    answer: str — full answer text
    sources: list[dict] — {text, href} source links
    url: str — Perplexity search URL
    title: str — page title
    follow_ups: list[str] — suggested follow-up questions
    mode: str — which search mode was used
"""

import random
import time
from typing import Optional

from .config import Config, get_config
from .drivers.base import BrowserDriver
from .drivers.webbridge import WebBridgeDriver
from .utils import compact_json, find_textbox, find_menuitem, find_button
from .utils.antidetect import (
    human_delay, micro_delay,
    human_paste, human_click, human_scroll,
    combined_snapshot_and_info, combined_extract,
)

SearchResult = dict


def _make_driver(config: Config, suffix: str) -> BrowserDriver:
    return WebBridgeDriver(
        url=config.webbridge_url,
        session=config.make_session(suffix),
    )


def _snapshot_compact(driver) -> str:
    resp = driver.snapshot()
    tree = resp.get("data", {}).get("tree", "")
    return compact_json(tree)


def _find_and_click_textbox(driver, s: str) -> Optional[str]:
    """Find textbox and click it with human-like timing."""
    ref = find_textbox(s)
    if ref:
        # Simulate human: brief pause before clicking
        micro_delay(0.1, 0.3)
        driver.click(ref)
        human_delay(0.3, 0.6)
    return ref


def _activate_mode(driver, mode_name: str) -> bool:
    """Activate a search mode via '/' menu with human timing."""
    # Type "/" — use CDP insertText (single char, no need for per-char typing)
    driver.cdp("Input.insertText", {"text": "/"})
    human_delay(0.8, 1.3)  # Wait for menu to appear (human reaction time)

    s = _snapshot_compact(driver)
    ref = find_menuitem(s, mode_name)
    if not ref:
        return False

    driver.click(ref)
    human_delay(0.8, 1.2)  # Wait for mode to activate
    return True


def _submit_query(driver):
    """Submit query with the three-event Enter combo."""
    driver.evaluate("""(() => {
        const el = document.querySelector("[contenteditable]");
        if (!el) return "no input";
        el.dispatchEvent(new InputEvent("beforeinput", {
            inputType: "insertText", data: "\\n", bubbles: true, cancelable: true
        }));
        el.dispatchEvent(new KeyboardEvent("keydown", {
            key: "Enter", code: "Enter", keyCode: 13, which: 13,
            bubbles: true, cancelable: true
        }));
        el.dispatchEvent(new KeyboardEvent("keyup", {
            key: "Enter", code: "Enter", keyCode: 13, which: 13,
            bubbles: true, cancelable: true
        }));
        return "submitted";
    })()""")


def _switch_to_tab(driver, tab_text: str):
    """Click a tab by text with human delay."""
    driver.evaluate(f"""(() => {{
        const tabs = Array.from(document.querySelectorAll("[role=tab]"));
        const t = tabs.find(t => t.innerText.includes("{tab_text}"));
        if (t) t.click();
    }})()""")
    micro_delay(0.2, 0.5)


def _is_search_done(driver) -> bool:
    """Check if search completed using consolidated JS."""
    info = combined_snapshot_and_info(driver)
    return info.get("done", False)


def _expand_answer(driver) -> bool:
    """Click '查看更多' to expand collapsed answer."""
    result = driver.evaluate("""(() => {
        const btns = Array.from(document.querySelectorAll("button"));
        const more = btns.find(b => b.innerText.includes("查看更多"));
        if (more) { more.click(); return "expanded"; }
        return "no expand";
    })()""")
    if result == "expanded":
        human_delay(0.5, 1.0)
        return True
    return False


def _extract_results(driver) -> dict:
    """Extract answer, sources, and follow-ups in one consolidated call."""
    # First switch to Links tab to load source data
    _switch_to_tab(driver, "链接")
    human_delay(0.3, 0.6)

    # Extract everything in ONE evaluate call
    data = combined_extract(driver)

    # Switch back to answer tab
    _switch_to_tab(driver, "答案")
    human_delay(0.2, 0.4)

    return data


# ──────────────────────────────────────────────
# Public search functions
# ──────────────────────────────────────────────

def search(
    query: str,
    config: Optional[Config] = None,
    driver: Optional[BrowserDriver] = None,
    wait_seconds: Optional[float] = None,
    expand: bool = True,
    new_tab: bool = True,
) -> SearchResult:
    """Standard Perplexity search with anti-detection.

    Args:
        query: Search query string.
        config: Configuration (uses default if None).
        driver: Browser driver (creates WebBridge if None).
        wait_seconds: Wait time for results (default from config).
        expand: Whether to expand collapsed answers.
        new_tab: Whether to open in a new browser tab.
    """
    cfg = config or get_config()
    drv = driver or _make_driver(cfg, "search")
    wait = wait_seconds or cfg.search_wait

    # 1. Navigate — human pause before starting
    human_delay(0.5, 1.5)
    drv.navigate(cfg.base_url, new_tab=new_tab, group_title=f"Search: {query[:50]}")
    time.sleep(cfg.page_load_wait)

    # 2. Find and click textbox
    s = _snapshot_compact(drv)
    ref = _find_and_click_textbox(drv, s)
    if not ref:
        return {"error": "Could not find textbox", "answer": None, "sources": [],
                "url": "", "title": "", "follow_ups": [], "mode": "search"}

    # 3. Type query with human-like input
    human_paste(drv, query, chunk_size=6, delay=0.06)
    human_delay(0.3, 0.8)  # Think before pressing Enter

    # 4. Submit
    _submit_query(drv)

    # 5. Wait for results — randomized check interval
    time.sleep(wait)

    # 6. Scroll down a bit (like a human reading), then expand
    human_scroll(drv, "down", amount=2, delay=0.4)
    if expand:
        _expand_answer(drv)
        human_delay(0.5, 1.0)

    # 7. Extract results (consolidated JS)
    data = _extract_results(drv)

    # 8. Get page URL/title (consolidated)
    info = combined_snapshot_and_info(drv)

    return {
        "answer": data.get("answer", ""),
        "sources": data.get("sources", []),
        "url": info.get("url", ""),
        "title": info.get("title", ""),
        "follow_ups": data.get("follow_ups", []),
        "mode": "search",
    }


def deep_research(
    query: str,
    config: Optional[Config] = None,
    driver: Optional[BrowserDriver] = None,
    wait_seconds: Optional[float] = None,
    new_tab: bool = True,
) -> SearchResult:
    """Deep Research mode — multi-step, longer answers, 60-120s."""
    cfg = config or get_config()
    drv = driver or _make_driver(cfg, "deep-research")
    wait = wait_seconds or cfg.deep_research_wait

    human_delay(0.5, 1.5)
    drv.navigate(cfg.base_url, new_tab=new_tab, group_title=f"DR: {query[:50]}")
    time.sleep(cfg.page_load_wait)

    s = _snapshot_compact(drv)
    ref = _find_and_click_textbox(drv, s)
    if not ref:
        return {"error": "Could not find textbox", "answer": None, "sources": [],
                "url": "", "title": "", "follow_ups": [], "mode": "deep_research"}

    if not _activate_mode(drv, "深度研究"):
        return {"error": "Could not activate Deep Research", "answer": None,
                "sources": [], "url": "", "title": "", "follow_ups": [], "mode": "deep_research"}

    # Type query after mode activation
    human_delay(0.3, 0.6)
    human_paste(drv, query, chunk_size=6, delay=0.06)
    human_delay(0.3, 0.8)
    _submit_query(drv)

    # Wait with periodic checks
    elapsed = 0
    while elapsed < wait:
        sleep_time = random.uniform(12, 18)
        time.sleep(sleep_time)
        elapsed += sleep_time
        if _is_search_done(drv):
            break

    # Scroll to read results
    human_scroll(drv, "down", amount=3, delay=0.5)

    data = _extract_results(drv)
    info = combined_snapshot_and_info(drv)

    return {
        "answer": data.get("answer", ""),
        "sources": data.get("sources", []),
        "url": info.get("url", ""),
        "title": info.get("title", ""),
        "follow_ups": data.get("follow_ups", []),
        "mode": "deep_research",
    }


def model_council(
    query: str,
    config: Optional[Config] = None,
    driver: Optional[BrowserDriver] = None,
    wait_seconds: Optional[float] = None,
    new_tab: bool = True,
) -> SearchResult:
    """Model Council mode — multiple models answer the same question."""
    cfg = config or get_config()
    drv = driver or _make_driver(cfg, "council")
    wait = wait_seconds or cfg.model_council_wait

    human_delay(0.5, 1.5)
    drv.navigate(cfg.base_url, new_tab=new_tab, group_title=f"Council: {query[:50]}")
    time.sleep(cfg.page_load_wait)

    s = _snapshot_compact(drv)
    ref = _find_and_click_textbox(drv, s)
    if not ref:
        return {"error": "Could not find textbox", "answer": None, "sources": [],
                "url": "", "title": "", "follow_ups": [], "mode": "model_council"}

    if not _activate_mode(drv, "模型委员会"):
        return {"error": "Could not activate Model Council", "answer": None,
                "sources": [], "url": "", "title": "", "follow_ups": [], "mode": "model_council"}

    human_delay(0.3, 0.6)
    human_paste(drv, query, chunk_size=6, delay=0.06)
    human_delay(0.3, 0.8)
    _submit_query(drv)
    time.sleep(wait)

    data = _extract_results(drv)
    info = combined_snapshot_and_info(drv)

    return {
        "answer": data.get("answer", ""),
        "sources": data.get("sources", []),
        "url": info.get("url", ""),
        "title": info.get("title", ""),
        "follow_ups": data.get("follow_ups", []),
        "mode": "model_council",
    }


def step_by_step(
    query: str,
    config: Optional[Config] = None,
    driver: Optional[BrowserDriver] = None,
    wait_seconds: Optional[float] = None,
    new_tab: bool = True,
) -> SearchResult:
    """Step-by-step Learning mode — guided, structured answers."""
    cfg = config or get_config()
    drv = driver or _make_driver(cfg, "stepbystep")
    wait = wait_seconds or cfg.step_by_step_wait

    human_delay(0.5, 1.5)
    drv.navigate(cfg.base_url, new_tab=new_tab, group_title=f"SbS: {query[:50]}")
    time.sleep(cfg.page_load_wait)

    s = _snapshot_compact(drv)
    ref = _find_and_click_textbox(drv, s)
    if not ref:
        return {"error": "Could not find textbox", "answer": None, "sources": [],
                "url": "", "title": "", "follow_ups": [], "mode": "step_by_step"}

    if not _activate_mode(drv, "逐步学习"):
        return {"error": "Could not activate Step-by-step", "answer": None,
                "sources": [], "url": "", "title": "", "follow_ups": [], "mode": "step_by_step"}

    human_delay(0.3, 0.6)
    human_paste(drv, query, chunk_size=6, delay=0.06)
    human_delay(0.3, 0.8)
    _submit_query(drv)
    time.sleep(wait)

    data = _extract_results(drv)
    info = combined_snapshot_and_info(drv)

    return {
        "answer": data.get("answer", ""),
        "sources": data.get("sources", []),
        "url": info.get("url", ""),
        "title": info.get("title", ""),
        "follow_ups": data.get("follow_ups", []),
        "mode": "step_by_step",
    }
