#!/usr/bin/env python3
"""
Perplexity Web Automation — Search & Extract via Kimi WebBridge.

Proven flow (tested 2026-08-29):
  1. navigate → perplexity.ai
  2. snapshot → find textbox @e ref
  3. click textbox (focus it)
  4. fill textbox with query
  5. dispatch beforeinput + keydown + keyup Enter events
  6. wait 10-15s for answer
  7. click "查看更多" if present (expand collapsed answer)
  8. extract answer text from main element
  9. click "链接" tab, extract all source links
  10. switch back to answer tab, extract follow-up questions

Usage:
    result = perplexity_search("What are the best AI coding agents in 2026?")
    print(result["answer"])
    print(result["sources"])
"""

import json
import subprocess
import time
import sys
import re

WEBBRIDGE_URL = "http://127.0.0.1:10086/command"
SESSION = "perplexity-search"


def wb(action, args=None):
    """Send a command to Kimi WebBridge."""
    payload = {"action": action, "session": SESSION}
    if args:
        payload["args"] = args
    result = subprocess.run(
        ["curl", "-s", "-X", "POST", WEBBRIDGE_URL,
         "-H", "Content-Type: application/json",
         "-d", json.dumps(payload)],
        capture_output=True, text=True, timeout=30
    )
    try:
        return json.loads(result.stdout)
    except Exception:
        return {"error": result.stdout}


def evaluate(code):
    """Run JS in the page and return the parsed value."""
    resp = wb("evaluate", {"code": code})
    val = resp.get("data", {}).get("value", "")
    if isinstance(val, str):
        try:
            return json.loads(val)
        except (json.JSONDecodeError, TypeError):
            return val
    return val


def find_textbox_ref():
    """Find the textbox @e ref from the accessibility tree snapshot.
    
    The textbox has:
      role: "textbox"
      name: "输入 @ 以使用连接器" (varies by locale)
      value: "\\n"
      ref: @eNN
    """
    resp = wb("snapshot", {})
    tree = resp.get("data", {}).get("tree", "")
    s = json.dumps(tree, ensure_ascii=False, separators=(",", ":"))
    
    # Pattern: "role":"textbox","name":"...","value":"...","ref":"@eNN"
    for m in re.finditer(
        r'"role":"textbox","name":"[^"]*","value":"[^"]*","ref":"(@e\d+)"', s
    ):
        return m.group(1)
    
    # Fallback: find any element with role textbox
    for m in re.finditer(r'"role":"textbox"[^}]*"ref":"(@e\d+)"', s):
        return m.group(1)
    
    # Reverse: find ref near "textbox"
    for m in re.finditer(r'"ref":"(@e\d+)"', s):
        ref = m.group(1)
        idx = s.find(ref)
        context = s[max(0, idx - 200):idx + 50]
        if '"role":"textbox"' in context:
            return ref
    
    return None


def find_button_ref(button_text):
    """Find a button's @e ref by its text content."""
    resp = wb("snapshot", {})
    tree = resp.get("data", {}).get("tree", "")
    s = json.dumps(tree, ensure_ascii=False, separators=(",", ":"))
    
    # Pattern: "role":"button","name":"TEXT","ref":"@eNN"
    for m in re.finditer(
        rf'"role":"button","name":"[^"]*{re.escape(button_text)}[^"]*","ref":"(@e\d+)"',
        s
    ):
        return m.group(1)
    
    return None


def perplexity_search(query, wait_seconds=15, expand=True, new_tab=True):
    """
    Search Perplexity AI and extract results.
    
    Args:
        query: Search query string
        wait_seconds: How long to wait for answer generation
        expand: Whether to click "查看更多" to expand full answer
        new_tab: Whether to open in a new tab
    
    Returns:
        dict with: answer, sources, url, title, follow_ups
    """
    # 1. Navigate to Perplexity
    wb("navigate", {
        "url": "https://www.perplexity.ai",
        "newTab": new_tab,
        "group_title": f"Perplexity: {query[:50]}"
    })
    time.sleep(4)
    
    # 2. Find textbox ref via snapshot
    textbox_ref = find_textbox_ref()
    if not textbox_ref:
        return {
            "error": "Could not find input textbox",
            "answer": None,
            "sources": [],
            "url": "",
            "title": "",
            "follow_ups": []
        }
    
    # 3. Click textbox to focus it (REQUIRED before fill)
    wb("click", {"selector": textbox_ref})
    time.sleep(0.5)
    
    # 4. Fill query
    fill_resp = wb("fill", {"selector": textbox_ref, "value": query})
    if not fill_resp.get("data", {}).get("success"):
        # Fallback: use CDP insertText
        wb("cdp", {"method": "Input.insertText", "params": {"text": query}})
    time.sleep(0.5)
    
    # 5. Submit via Enter key (three-event combo: beforeinput + keydown + keyup)
    evaluate("""(() => {
        const el = document.querySelector("[contenteditable]");
        if (!el) return "no input element";
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
        return "enter dispatched";
    })()""")
    
    # 6. Wait for results
    time.sleep(wait_seconds)
    
    # 7. Verify we're on a search results page
    page_info = evaluate("""(() => {
        return JSON.stringify({url: location.href, title: document.title});
    })()""")
    
    url = page_info.get("url", "") if isinstance(page_info, dict) else ""
    title = page_info.get("title", "") if isinstance(page_info, dict) else ""
    
    if "/search/" not in url:
        return {
            "error": "Search did not trigger (stayed on homepage)",
            "answer": None,
            "sources": [],
            "url": url,
            "title": title,
            "follow_ups": []
        }
    
    # 8. Expand answer if collapsed (click "查看更多")
    if expand:
        evaluate("""(() => {
            const btns = Array.from(document.querySelectorAll("button"));
            const more = btns.find(b => b.innerText.includes("查看更多"));
            if (more) { more.click(); return "expanded"; }
            return "no expand button";
        })()""")
        time.sleep(2)
    
    # 9. Extract answer text
    answer_data = evaluate("""(() => {
        const main = document.querySelector("main");
        if (!main) return JSON.stringify({text: "", url: location.href});
        return JSON.stringify({
            text: main.innerText,
            url: location.href,
            title: document.title
        });
    })()""")
    
    answer_text = ""
    if isinstance(answer_data, dict):
        answer_text = answer_data.get("text", "")
        url = answer_data.get("url", url)
        title = answer_data.get("title", title)
    
    # 10. Extract sources from links tab
    evaluate("""(() => {
        const tabs = Array.from(document.querySelectorAll("[role=tab]"));
        const linksTab = tabs.find(t => t.innerText.includes("链接"));
        if (linksTab) { linksTab.click(); return "clicked"; }
        return "not found";
    })()""")
    time.sleep(1)
    
    sources_raw = evaluate("""(() => {
        const main = document.querySelector("main");
        if (!main) return JSON.stringify([]);
        const links = Array.from(main.querySelectorAll("a[href]"))
            .map(a => ({
                text: a.textContent.trim().substring(0, 200),
                href: a.href
            }))
            .filter(l => l.href.startsWith("http") &&
                        !l.href.includes("perplexity.ai") &&
                        l.text.length > 2);
        const seen = new Set();
        const unique = links.filter(l => {
            if (seen.has(l.href)) return false;
            seen.add(l.href);
            return true;
        });
        return JSON.stringify(unique);
    })()""")
    
    sources = sources_raw if isinstance(sources_raw, list) else []
    
    # 11. Switch back to answer tab
    evaluate("""(() => {
        const tabs = Array.from(document.querySelectorAll("[role=tab]"));
        const answerTab = tabs.find(t => t.innerText.includes("答案"));
        if (answerTab) { answerTab.click(); return "clicked"; }
        return "not found";
    })()""")
    time.sleep(1)
    
    # 12. Extract follow-up questions
    follow_ups_raw = evaluate("""(() => {
        const btns = Array.from(document.querySelectorAll("button"));
        const followUps = btns
            .map(b => b.innerText.trim())
            .filter(t => t.length > 20 && t.length < 200 &&
                        !t.includes("分享") && !t.includes("搜索") &&
                        !t.includes("Computer") && !t.includes("Ming Fang") &&
                        !t.includes("Pro") && !t.includes("添加") &&
                        !t.includes("展开") && !t.includes("完成") &&
                        !t.includes("来源") && !t.includes("会话"));
        return JSON.stringify(followUps.slice(0, 5));
    })()""")
    
    follow_ups = follow_ups_raw if isinstance(follow_ups_raw, list) else []
    
    return {
        "answer": answer_text,
        "sources": sources,
        "url": url,
        "title": title,
        "follow_ups": follow_ups
    }


def perplexity_batch_search(queries, wait_seconds=15):
    """Search multiple queries sequentially."""
    results = []
    for i, query in enumerate(queries):
        print(f"[{i+1}/{len(queries)}] {query[:60]}...", file=sys.stderr)
        result = perplexity_search(query, wait_seconds=wait_seconds)
        results.append(result)
        time.sleep(2)
    return results


def perplexity_follow_up(follow_up_text, wait_seconds=15):
    """Click a follow-up question on the current page."""
    evaluate(f"""(() => {{
        const btns = Array.from(document.querySelectorAll("button"));
        const target = btns.find(b => b.innerText.includes({json.dumps(follow_up_text)}));
        if (target) {{ target.click(); return "clicked"; }}
        return "not found";
    }})()""")
    time.sleep(wait_seconds)
    
    # Expand and extract
    evaluate("""(() => {
        const btns = Array.from(document.querySelectorAll("button"));
        const more = btns.find(b => b.innerText.includes("查看更多"));
        if (more) { more.click(); return "expanded"; }
        return "no expand";
    })()""")
    time.sleep(2)
    
    return evaluate("""(() => {
        const main = document.querySelector("main");
        return main ? main.innerText : "no main";
    })()""")


def _activate_search_mode(mode_name, session=SESSION):
    """Activate a search mode via '/' menu.
    
    Args:
        mode_name: Partial match for the mode, e.g. "深度研究", "模型委员会", "逐步学习"
    """
    # Type '/' to trigger search mode selector
    wb("cdp", {"method": "Input.insertText", "params": {"text": "/"}})
    time.sleep(1)
    
    # Snapshot to find menu items
    resp = wb("snapshot", {})
    tree = resp.get("data", {}).get("tree", "")
    s = json.dumps(tree, ensure_ascii=False, separators=(",", ":"))
    
    # Find the menuitem matching mode_name
    target_ref = None
    for m in re.finditer(
        rf'"role":"menuitem","name":"([^"]*{re.escape(mode_name)}[^"]*)","ref":"(@e\d+)"',
        s
    ):
        target_ref = m.group(2)
        break
    
    if not target_ref:
        return False
    
    # Click the mode
    wb("click", {"selector": target_ref})
    time.sleep(1)
    return True


def _select_model(model_name, session=SESSION):
    """Switch the AI model via the model selector dropdown.
    
    Args:
        model_name: Partial match for the model name, e.g. "Claude", "GPT", "Sonnet"
    """
    # Find and click the model selector button (has a model name + "⌵")
    resp = wb("snapshot", {})
    tree = resp.get("data", {}).get("tree", "")
    s = json.dumps(tree, ensure_ascii=False, separators=(",", ":"))
    
    # Find model selector button - it's a button with the current model name
    model_ref = None
    for m in re.finditer(r'"role":"button","name":"([^"]{2,40})","ref":"(@e\d+)"', s):
        name, ref = m.groups()
        # Model buttons typically contain known model names
        if any(mn in name for mn in ["Grok", "Claude", "GPT", "Sonnet", "Opus", "o3", "o4", "Gemini"]):
            model_ref = ref
            break
    
    if not model_ref:
        return False
    
    # Click the model selector
    wb("click", {"selector": model_ref})
    time.sleep(1)
    
    # Snapshot again to find the dropdown options
    resp = wb("snapshot", {})
    tree = resp.get("data", {}).get("tree", "")
    s = json.dumps(tree, ensure_ascii=False, separators=(",", ":"))
    
    # Find the target model in dropdown
    target_ref = None
    for m in re.finditer(
        rf'"role":"(option|menuitem|button)","name":"([^"]*{re.escape(model_name)}[^"]*)","ref":"(@e\d+)"',
        s
    ):
        target_ref = m.group(3)
        break
    
    if not target_ref:
        # Close the dropdown by pressing Escape
        wb("cdp", {"method": "Input.dispatchKeyEvent", 
                    "params": {"type": "keyDown", "key": "Escape"}})
        return False
    
    wb("click", {"selector": target_ref})
    time.sleep(1)
    return True


def perplexity_deep_research(query, wait_seconds=90, new_tab=True):
    """
    Search Perplexity using Deep Research mode.
    
    Deep Research uses multiple steps (4+) and produces longer, more
    detailed answers. Takes 60-120 seconds.
    
    Args:
        query: Search query string
        wait_seconds: How long to wait (default 90, Deep Research is slow)
        new_tab: Whether to open in a new tab
    
    Returns:
        dict with: answer, sources, url, title, follow_ups, mode
    """
    # 1. Navigate
    wb("navigate", {
        "url": "https://www.perplexity.ai",
        "newTab": new_tab,
        "group_title": f"Deep Research: {query[:50]}"
    })
    time.sleep(4)
    
    # 2. Find and click textbox
    textbox_ref = find_textbox_ref()
    if not textbox_ref:
        return {"error": "Could not find textbox", "answer": None, "sources": [],
                "url": "", "title": "", "follow_ups": [], "mode": "deep_research"}
    wb("click", {"selector": textbox_ref})
    time.sleep(0.5)
    
    # 3. Activate Deep Research mode
    if not _activate_search_mode("深度研究"):
        return {"error": "Could not activate Deep Research mode", "answer": None,
                "sources": [], "url": "", "title": "", "follow_ups": [], "mode": "deep_research"}
    
    wb("cdp", {"method": "Input.insertText", "params": {"text": query}})
    time.sleep(0.5)
    
    # 5. Submit
    evaluate("""(() => {
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
    
    # 6. Wait for results (Deep Research is slow)
    # Check periodically for completion
    for i in range(wait_seconds // 15):
        time.sleep(15)
        status = evaluate("""(() => {
            const btns = Array.from(document.querySelectorAll("button"));
            const done = btns.find(b => b.innerText.includes("已完成"));
            const steps = done ? done.innerText : "in progress";
            return JSON.stringify({url: location.href, status: steps});
        })()""")
        if isinstance(status, dict) and "/search/" in status.get("url", ""):
            # Check if it shows completed steps
            status_text = status.get("status", "")
            if "已完成" in status_text:
                break
    
    # 7. Extract results (same as regular search but no expand needed)
    page_info = evaluate("""(() => {
        return JSON.stringify({url: location.href, title: document.title});
    })()""")
    url = page_info.get("url", "") if isinstance(page_info, dict) else ""
    title = page_info.get("title", "") if isinstance(page_info, dict) else ""
    
    # 8. Extract answer
    answer_data = evaluate("""(() => {
        const main = document.querySelector("main");
        if (!main) return JSON.stringify({text: ""});
        return JSON.stringify({text: main.innerText, url: location.href, title: document.title});
    })()""")
    answer_text = answer_data.get("text", "") if isinstance(answer_data, dict) else ""
    url = (answer_data.get("url", url) if isinstance(answer_data, dict) else url)
    
    # 9. Extract sources
    evaluate("""(() => {
        const tabs = Array.from(document.querySelectorAll("[role=tab]"));
        const t = tabs.find(t => t.innerText.includes("链接"));
        if (t) t.click();
    })()""")
    time.sleep(1)
    
    sources_raw = evaluate("""(() => {
        const main = document.querySelector("main");
        if (!main) return JSON.stringify([]);
        const links = Array.from(main.querySelectorAll("a[href]"))
            .map(a => ({text: a.textContent.trim().substring(0, 200), href: a.href}))
            .filter(l => l.href.startsWith("http") && !l.href.includes("perplexity.ai") && l.text.length > 2);
        const seen = new Set();
        return JSON.stringify(links.filter(l => {if(seen.has(l.href))return false;seen.add(l.href);return true;}));
    })()""")
    sources = sources_raw if isinstance(sources_raw, list) else []
    
    # 10. Switch back to answer tab
    evaluate("""(() => {
        const tabs = Array.from(document.querySelectorAll("[role=tab]"));
        const t = tabs.find(t => t.innerText.includes("答案"));
        if (t) t.click();
    })()""")
    time.sleep(1)
    
    # 11. Follow-ups
    follow_ups_raw = evaluate("""(() => {
        const btns = Array.from(document.querySelectorAll("button"));
        return JSON.stringify(btns.map(b=>b.innerText.trim())
            .filter(t=>t.length>20&&t.length<200
                &&!t.includes("分享")&&!t.includes("搜索")&&!t.includes("Computer")
                &&!t.includes("Ming")&&!t.includes("Pro")&&!t.includes("添加")
                &&!t.includes("展开")&&!t.includes("完成")&&!t.includes("来源")
                &&!t.includes("会话")).slice(0,5));
    })()""")
    follow_ups = follow_ups_raw if isinstance(follow_ups_raw, list) else []
    
    return {
        "answer": answer_text,
        "sources": sources,
        "url": url,
        "title": title,
        "follow_ups": follow_ups,
        "mode": "deep_research"
    }


def perplexity_model_council(query, wait_seconds=25, new_tab=True):
    """
    Search Perplexity using Model Council mode (multiple models answer).
    """
    wb("navigate", {
        "url": "https://www.perplexity.ai",
        "newTab": new_tab,
        "group_title": f"Model Council: {query[:50]}"
    })
    time.sleep(4)
    
    textbox_ref = find_textbox_ref()
    if not textbox_ref:
        return {"error": "Could not find textbox", "answer": None, "sources": [],
                "url": "", "title": "", "follow_ups": [], "mode": "model_council"}
    wb("click", {"selector": textbox_ref})
    time.sleep(0.5)
    
    if not _activate_search_mode("模型委员会"):
        return {"error": "Could not activate Model Council", "answer": None,
                "sources": [], "url": "", "title": "", "follow_ups": [], "mode": "model_council"}
    
    # Clear input via fill with empty string
    textbox_ref2 = find_textbox_ref()
    if textbox_ref2:
        wb("fill", {"selector": textbox_ref2, "value": ""})
    time.sleep(0.3)
    time.sleep(0.3)
    wb("cdp", {"method": "Input.insertText", "params": {"text": query}})
    time.sleep(0.5)
    
    evaluate("""(() => {
        const el = document.querySelector("[contenteditable]");
        if (!el) return;
        el.dispatchEvent(new InputEvent("beforeinput",{inputType:"insertText",data:"\\n",bubbles:true,cancelable:true}));
        el.dispatchEvent(new KeyboardEvent("keydown",{key:"Enter",code:"Enter",keyCode:13,which:13,bubbles:true,cancelable:true}));
        el.dispatchEvent(new KeyboardEvent("keyup",{key:"Enter",code:"Enter",keyCode:13,which:13,bubbles:true,cancelable:true}));
    })()""")
    
    time.sleep(wait_seconds)
    
    # Extract results
    page_info = evaluate("(() => JSON.stringify({url:location.href,title:document.title}))()")
    url = page_info.get("url", "") if isinstance(page_info, dict) else ""
    title = page_info.get("title", "") if isinstance(page_info, dict) else ""
    
    answer_data = evaluate("(() => { const m=document.querySelector('main'); return JSON.stringify({text:m?m.innerText:''}); })()")
    answer_text = answer_data.get("text", "") if isinstance(answer_data, dict) else ""
    
    sources = []
    follow_ups = []
    
    return {
        "answer": answer_text, "sources": sources, "url": url,
        "title": title, "follow_ups": follow_ups, "mode": "model_council"
    }


def perplexity_step_by_step(query, wait_seconds=20, new_tab=True):
    """
    Search Perplexity using Step-by-step Learning mode.
    """
    wb("navigate", {
        "url": "https://www.perplexity.ai",
        "newTab": new_tab,
        "group_title": f"Step-by-step: {query[:50]}"
    })
    time.sleep(4)
    
    textbox_ref = find_textbox_ref()
    if not textbox_ref:
        return {"error": "Could not find textbox", "answer": None, "sources": [],
                "url": "", "title": "", "follow_ups": [], "mode": "step_by_step"}
    wb("click", {"selector": textbox_ref})
    time.sleep(0.5)
    
    if not _activate_search_mode("逐步学习"):
        return {"error": "Could not activate Step-by-step", "answer": None,
                "sources": [], "url": "", "title": "", "follow_ups": [], "mode": "step_by_step"}
    
    # Clear input via fill with empty string
    textbox_ref2 = find_textbox_ref()
    if textbox_ref2:
        wb("fill", {"selector": textbox_ref2, "value": ""})
    time.sleep(0.3)
    time.sleep(0.3)
    wb("cdp", {"method": "Input.insertText", "params": {"text": query}})
    time.sleep(0.5)
    
    evaluate("""(() => {
        const el = document.querySelector("[contenteditable]");
        if (!el) return;
        el.dispatchEvent(new InputEvent("beforeinput",{inputType:"insertText",data:"\\n",bubbles:true,cancelable:true}));
        el.dispatchEvent(new KeyboardEvent("keydown",{key:"Enter",code:"Enter",keyCode:13,which:13,bubbles:true,cancelable:true}));
        el.dispatchEvent(new KeyboardEvent("keyup",{key:"Enter",code:"Enter",keyCode:13,which:13,bubbles:true,cancelable:true}));
    })()""")
    
    time.sleep(wait_seconds)
    
    page_info = evaluate("(() => JSON.stringify({url:location.href,title:document.title}))()")
    url = page_info.get("url", "") if isinstance(page_info, dict) else ""
    title = page_info.get("title", "") if isinstance(page_info, dict) else ""
    
    answer_data = evaluate("(() => { const m=document.querySelector('main'); return JSON.stringify({text:m?m.innerText:''}); })()")
    answer_text = answer_data.get("text", "") if isinstance(answer_data, dict) else ""
    
    return {
        "answer": answer_text, "sources": [], "url": url,
        "title": title, "follow_ups": [], "mode": "step_by_step"
    }


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python perplexity_search.py 'your query here'")
        sys.exit(1)
    
    query = " ".join(sys.argv[1:])
    result = perplexity_search(query)
    
    if result.get("error"):
        print(f"ERROR: {result['error']}")
        sys.exit(1)
    
    print(f"URL: {result.get('url', 'N/A')}")
    print(f"Title: {result.get('title', 'N/A')}")
    print(f"\n{'='*60}")
    print("ANSWER:")
    print(result.get("answer", "No answer"))
    print(f"\n{'='*60}")
    sources = result.get("sources", [])
    print(f"SOURCES ({len(sources)}):")
    for i, s in enumerate(sources):
        print(f"  {i+1}. {s.get('text', '')[:80]} -> {s.get('href', '')}")
    print(f"\n{'='*60}")
    print("FOLLOW-UP QUESTIONS:")
    for q in result.get("follow_ups", []):
        print(f"  - {q}")
