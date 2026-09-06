---
name: perplexity-web-automation
description: |
  Automate Perplexity AI search via Kimi WebBridge — search, extract answers, sources, and structured data from Perplexity's web interface. Use when the user wants to batch search Perplexity, extract research results, or automate Perplexity workflows.
metadata:
  version: "0.1.0"
  requires: ["kimi-webbridge"]
---

# Perplexity Web Automation Skill

Automate search and extraction from Perplexity AI (Pro account) via Kimi WebBridge browser control.

## Prerequisites

- Kimi WebBridge daemon running (`~/.kimi-webbridge/bin/kimi-webbridge start`)
- Perplexity Pro account logged in (uses user's existing session)
- Session name: `perplexity-search` (consistent across all operations)

## Core Workflow

### 1. Search (Proven 2026-08-29)

```bash
# Navigate to Perplexity
curl -s -X POST http://127.0.0.1:10086/command \
  -H 'Content-Type: application/json' \
  -d '{"action":"navigate","args":{"url":"https://www.perplexity.ai","newTab":true,"group_title":"Perplexity Search"},"session":"perplexity-search"}'
# Wait 4s for page load

# Snapshot to get @e refs (use compact JSON for regex)
curl -s -X POST http://127.0.0.1:10086/command \
  -H 'Content-Type: application/json' \
  -d '{"action":"snapshot","args":{},"session":"perplexity-search"}'
# Textbox: role="textbox", name="问任何事情..." or "输入 @ 以使用连接器"
# Search button: role="button", name="搜索"

# Click textbox FIRST (required to focus it)
curl -s -X POST http://127.0.0.1:10086/command \
  -H 'Content-Type: application/json' \
  -d '{"action":"click","args":{"selector":"@eNN"},"session":"perplexity-search"}'

# Fill query
curl -s -X POST http://127.0.0.1:10086/command \
  -H 'Content-Type: application/json' \
  -d '{"action":"fill","args":{"selector":"@eNN","value":"YOUR QUERY"},"session":"perplexity-search"}'
# Returns: {"mode":"contenteditable","success":true,"tag":"DIV"}

# Submit via three-event Enter combo (CRITICAL — single keydown doesn't work)
curl -s -X POST http://127.0.0.1:10086/command \
  -H 'Content-Type: application/json' \
  -d '{"action":"evaluate","args":{"code":"(() => { const el = document.querySelector(\"[contenteditable]\"); el.dispatchEvent(new InputEvent(\"beforeinput\",{inputType:\"insertText\",data:\"\\n\",bubbles:true,cancelable:true})); el.dispatchEvent(new KeyboardEvent(\"keydown\",{key:\"Enter\",code:\"Enter\",keyCode:13,which:13,bubbles:true,cancelable:true})); el.dispatchEvent(new KeyboardEvent(\"keyup\",{key:\"Enter\",code:\"Enter\",keyCode:13,which:13,bubbles:true,cancelable:true})); })()"},"session":"perplexity-search"}'
```

### 2. Wait & Expand

```bash
# Wait 10-15 seconds for answer generation
sleep 12

# Click "查看更多" to expand full answer
curl -s -X POST http://127.0.0.1:10086/command \
  -H 'Content-Type: application/json' \
  -d '{"action":"evaluate","args":{"code":"(() => { const btns = Array.from(document.querySelectorAll(\"button\")); const more = btns.find(b=>b.innerText.includes(\"查看更多\")); if(more) { more.click(); return \"clicked\"; } return \"no expand button\"; })()"},"session":"perplexity-search"}'
```

### 3. Extract Answer

```bash
# Get full answer text from main area
curl -s -X POST http://127.0.0.1:10086/command \
  -H 'Content-Type: application/json' \
  -d '{"action":"evaluate","args":{"code":"(() => { const main = document.querySelector(\"main\"); return main ? JSON.stringify({text:main.innerText, url:location.href, title:document.title}) : \"no main\"; })()"},"session":"perplexity-search"}'
```

### 4. Extract Sources

```bash
# Click "链接" tab
curl -s -X POST http://127.0.0.1:10086/command \
  -H 'Content-Type: application/json' \
  -d '{"action":"evaluate","args":{"code":"(() => { const tabs = Array.from(document.querySelectorAll(\"[role=tab]\")); const linksTab = tabs.find(t=>t.innerText.includes(\"链接\")); if(linksTab) { linksTab.click(); return \"clicked\"; } return \"not found\"; })()"},"session":"perplexity-search"}'

# Extract all source links
curl -s -X POST http://127.0.0.1:10086/command \
  -H 'Content-Type: application/json' \
  -d '{"action":"evaluate","args":{"code":"(() => { const main = document.querySelector(\"main\"); const links = Array.from(main.querySelectorAll(\"a[href]\")).map(a=>({text:a.textContent.trim().substring(0,200),href:a.href})).filter(l=>l.href.startsWith(\"http\")&&!l.href.includes(\"perplexity.ai\")&&l.text.length>2); return JSON.stringify(links); })()"},"session":"perplexity-search"}'
```

### 5. Follow-up Questions

```bash
# Click a follow-up suggestion (by text content)
curl -s -X POST http://127.0.0.1:10086/command \
  -H 'Content-Type: application/json' \
  -d '{"action":"evaluate","args":{"code":"(() => { const btns = Array.from(document.querySelectorAll(\"button\")); const target = btns.find(b=>b.innerText.includes(\"FOLLOW UP TEXT\")); if(target) { target.click(); return \"clicked\"; } return \"not found\"; })()"},"session":"perplexity-search"}'
```

### 6. New Search (Continue in Same Tab)

```bash
# Type in the follow-up input at the bottom
curl -s -X POST http://127.0.0.1:10086/command \
  -H 'Content-Type: application/json' \
  -d '{"action":"evaluate","args":{"code":"(() => { const input = document.querySelector(\"[contenteditable]\"); if(input) { input.focus(); input.textContent = \"NEW QUERY\"; input.dispatchEvent(new Event(\"input\",{bubbles:true})); return \"filled\"; } return \"not found\"; })()"},"session":"perplexity-search"}'
```

## Key DOM Patterns

| Element | Selector Strategy | Notes |
|---------|-------------------|-------|
| Input box | `document.querySelector("[contenteditable]")` | Always one contenteditable div |
| Search button | `button` with text "搜索" | Sometimes unreliable; prefer Enter key |
| Model selector | `button` with model name text | Dropdown requires CDP click |
| Answer tab | `[role=tab]` with text "答案" | Default active |
| Links tab | `[role=tab]` with text "链接" | Shows sources |
| Images tab | `[role=tab]` with text "图片" | Image results |
| Expand answer | `button` with text "查看更多" | Collapsed by default! |
| Source count | `button` with text "N 个来源" | Inline badge |
| Follow-up Qs | `button` with question text | Below answer |

## Python Helper Script

See `scripts/perplexity_search.py` for a reusable function that:
1. Takes a query string
2. Searches Perplexity
3. Waits for results
4. Expands the answer
5. Returns `{answer, sources, url, follow_ups}`

## Known Limitations

1. **Answer truncation**: Answers are collapsed by default; must click "查看更多"
2. **Element refs change**: Every session gets different @e refs; use role+name or JS selectors
3. **Model dropdown**: React synthetic events; may need CDP for model switching
4. **Rate limits**: Perplexity Pro has usage limits; batch carefully
5. **No Deep Research automation yet**: Need to map the Deep Research UI flow
6. **Focus modes not mapped**: Academic, Writing, Math modes need exploration
7. **File upload not mapped**: Need to explore the upload flow

## Session Management

- Use one session name (`perplexity-search`) for all Perplexity operations
- Close session when done: `close_session` action
- Tabs accumulate in the session's group; close old tabs to avoid clutter
