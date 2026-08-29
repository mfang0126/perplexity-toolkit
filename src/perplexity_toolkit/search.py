"""Core Perplexity search functions.

Each function returns a SearchResult dict with:
    answer: str — full answer text
    sources: list[dict] — {text, href} source links
    url: str — Perplexity search URL
    title: str — page title
    follow_ups: list[str] — suggested follow-up questions
    mode: str — which search mode was used
"""

import time
from typing import Optional

from .config import Config, get_config
from .drivers.base import BrowserDriver
from .drivers.webbridge import WebBridgeDriver
from .utils import (
    compact_json,
    find_textbox,
    find_menuitem,
    find_button,
    find_tab,
    extract_answer_text,
    extract_sources,
    extract_follow_ups,
    submit_query,
)

SearchResult = dict


def _make_driver(config: Config, suffix: str) -> BrowserDriver:
    return WebBridgeDriver(
        url=config.webbridge_url,
        session=config.make_session(suffix),
    )


def _extract_page_info(driver) -> dict:
    result = driver.evaluate(
        '(() => JSON.stringify({url:location.href,title:document.title}))()'
    )
    return result if isinstance(result, dict) else {"url": "", "title": ""}


def _snapshot_compact(driver) -> str:
    resp = driver.snapshot()
    tree = resp.get("data", {}).get("tree", "")
    return compact_json(tree)


def _find_and_click_textbox(driver, s: str) -> Optional[str]:
    ref = find_textbox(s)
    if ref:
        driver.click(ref)
        time.sleep(0.3)
    return ref


def _activate_mode(driver, mode_name: str) -> bool:
    """Activate a search mode via '/' menu."""
    driver.cdp("Input.insertText", {"text": "/"})
    time.sleep(1)
    s = _snapshot_compact(driver)
    ref = find_menuitem(s, mode_name)
    if not ref:
        return False
    driver.click(ref)
    time.sleep(1)
    return True


def _switch_to_tab(driver, tab_text: str):
    """Click a tab (答案/链接/图片) by text."""
    driver.evaluate(f"""(() => {{
        const tabs = Array.from(document.querySelectorAll("[role=tab]"));
        const t = tabs.find(t => t.innerText.includes("{tab_text}"));
        if (t) t.click();
    }})()""")


def _expand_answer(driver) -> bool:
    """Click '查看更多' to expand collapsed answer."""
    result = driver.evaluate("""(() => {
        const btns = Array.from(document.querySelectorAll("button"));
        const more = btns.find(b => b.innerText.includes("查看更多"));
        if (more) { more.click(); return "expanded"; }
        return "no expand";
    })()""")
    return result == "expanded"


def _is_search_done(driver) -> bool:
    """Check if the search page shows completed status."""
    result = driver.evaluate("""(() => {
        const btns = Array.from(document.querySelectorAll("button"));
        const done = btns.find(b => b.innerText.includes("已完成"));
        return done ? "done" : "pending";
    })()""")
    return result == "done"


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
    """Standard Perplexity search.

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

    # Navigate
    drv.navigate(cfg.base_url, new_tab=new_tab, group_title=f"Search: {query[:50]}")
    time.sleep(cfg.page_load_wait)

    # Find and click textbox
    s = _snapshot_compact(drv)
    ref = _find_and_click_textbox(drv, s)
    if not ref:
        return {"error": "Could not find textbox", "answer": None, "sources": [],
                "url": "", "title": "", "follow_ups": [], "mode": "search"}

    # Fill query
    drv.fill(ref, query)
    time.sleep(cfg.action_wait)

    # Submit
    submit_query(drv)
    time.sleep(wait)

    # Expand if needed
    if expand:
        _expand_answer(drv)
        time.sleep(1)

    # Extract results
    info = _extract_page_info(drv)
    answer = extract_answer_text(drv)

    # Sources
    _switch_to_tab(drv, "链接")
    time.sleep(cfg.action_wait)
    sources = extract_sources(drv)
    _switch_to_tab(drv, "答案")
    time.sleep(cfg.action_wait)

    return {
        "answer": answer,
        "sources": sources,
        "url": info.get("url", ""),
        "title": info.get("title", ""),
        "follow_ups": extract_follow_ups(drv),
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

    # Type query (append to existing "/" prefix — Perplexity handles it)
    drv.cdp("Input.insertText", {"text": query})
    time.sleep(cfg.action_wait)
    submit_query(drv)

    # Wait with periodic checks
    for _ in range(int(wait / 15)):
        time.sleep(15)
        if _is_search_done(drv):
            break

    answer = extract_answer_text(drv)
    _switch_to_tab(drv, "链接")
    time.sleep(cfg.action_wait)
    sources = extract_sources(drv)
    _switch_to_tab(drv, "答案")
    time.sleep(cfg.action_wait)

    info = _extract_page_info(drv)
    return {
        "answer": answer,
        "sources": sources,
        "url": info.get("url", ""),
        "title": info.get("title", ""),
        "follow_ups": extract_follow_ups(drv),
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

    drv.cdp("Input.insertText", {"text": query})
    time.sleep(cfg.action_wait)
    submit_query(drv)
    time.sleep(wait)

    info = _extract_page_info(drv)
    return {
        "answer": extract_answer_text(drv),
        "sources": extract_sources(drv),
        "url": info.get("url", ""),
        "title": info.get("title", ""),
        "follow_ups": extract_follow_ups(drv),
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

    drv.cdp("Input.insertText", {"text": query})
    time.sleep(cfg.action_wait)
    submit_query(drv)
    time.sleep(wait)

    info = _extract_page_info(drv)
    return {
        "answer": extract_answer_text(drv),
        "sources": extract_sources(drv),
        "url": info.get("url", ""),
        "title": info.get("title", ""),
        "follow_ups": extract_follow_ups(drv),
        "mode": "step_by_step",
    }
