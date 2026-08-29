"""DOM parsing and interaction utilities for Perplexity."""

import json
import re
from typing import Optional


def compact_json(tree) -> str:
    """Convert accessibility tree to compact JSON string (no spaces)."""
    return json.dumps(tree, ensure_ascii=False, separators=(",", ":"))


def find_ref_by_role(s: str, role: str, name_pattern: str = "") -> Optional[str]:
    """Find an @e ref by role and optional name pattern in compact JSON."""
    if name_pattern:
        pat = rf'"role":"{role}","name":"[^"]*{re.escape(name_pattern)}[^"]*","ref":"(@e\d+)"'
    else:
        pat = rf'"role":"{role}"[^}}]*"ref":"(@e\d+)"'
    for m in re.finditer(pat, s):
        return m.group(1)
    return None


def find_textbox(s: str) -> Optional[str]:
    """Find the Perplexity search textbox @e ref."""
    # Primary: textbox with value field
    for m in re.finditer(
        r'"role":"textbox","name":"[^"]*","value":"[^"]*","ref":"(@e\d+)"', s
    ):
        return m.group(1)
    # Fallback: any textbox
    return find_ref_by_role(s, "textbox")


def find_menuitem(s: str, text: str) -> Optional[str]:
    """Find a menuitem @e ref by partial text match."""
    for m in re.finditer(
        rf'"role":"menuitem","name":"([^"]*{re.escape(text)}[^"]*)","ref":"(@e\d+)"',
        s,
    ):
        return m.group(2)
    return None


def find_button(s: str, text: str) -> Optional[str]:
    """Find a button @e ref by partial text match."""
    for m in re.finditer(
        rf'"role":"button","name":"[^"]*{re.escape(text)}[^"]*","ref":"(@e\d+)"',
        s,
    ):
        return m.group(1)
    return None


def find_tab(s: str, text: str) -> Optional[str]:
    """Find a tab @e ref by partial text match."""
    for m in re.finditer(
        rf'"role":"tab","name":"[^"]*{re.escape(text)}[^"]*","ref":"(@e\d+)"',
        s,
    ):
        return m.group(1)
    return None


def extract_answer_text(driver) -> str:
    """Extract the answer text from the main content area."""
    result = driver.evaluate("""(() => {
        const main = document.querySelector("main");
        return main ? main.innerText : "";
    })()""")
    return result if isinstance(result, str) else ""


def extract_sources(driver) -> list:
    """Extract source links from the Links tab."""
    result = driver.evaluate("""(() => {
        const main = document.querySelector("main");
        if (!main) return JSON.stringify([]);
        const links = Array.from(main.querySelectorAll("a[href]"))
            .map(a => ({text: a.textContent.trim().substring(0, 200), href: a.href}))
            .filter(l => l.href.startsWith("http") && !l.href.includes("perplexity.ai") && l.text.length > 2);
        const seen = new Set();
        return JSON.stringify(links.filter(l => {
            if (seen.has(l.href)) return false;
            seen.add(l.href);
            return true;
        }));
    })()""")
    return result if isinstance(result, list) else []


def extract_follow_ups(driver) -> list:
    """Extract follow-up question suggestions."""
    result = driver.evaluate("""(() => {
        const btns = Array.from(document.querySelectorAll("button"));
        return JSON.stringify(btns.map(b => b.innerText.trim())
            .filter(t => t.length > 20 && t.length < 200 &&
                !t.includes("分享") && !t.includes("搜索") && !t.includes("Computer") &&
                !t.includes("Ming") && !t.includes("Pro") && !t.includes("添加") &&
                !t.includes("展开") && !t.includes("完成") && !t.includes("来源") &&
                !t.includes("会话")).slice(0, 5));
    })()""")
    return result if isinstance(result, list) else []


def submit_query(driver):
    """Submit the current query via Enter key (three-event combo)."""
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
