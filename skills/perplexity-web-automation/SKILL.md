---
name: perplexity-web-automation
description: |
  Use when the user asks to search Perplexity through a real browser. Automate Perplexity via Kimi WebBridge, extract answers and sources, and preserve task-level session state.
metadata:
  version: "0.1.1"
  requires: ["kimi-webbridge"]
---

# Perplexity Web Automation Skill

Automate search and extraction from Perplexity AI (Pro account) via Kimi WebBridge browser control.

## Prerequisites

- Kimi WebBridge daemon running (`~/.kimi-webbridge/bin/kimi-webbridge start`)
- Perplexity Pro account logged in (uses user's existing session)
- Choose one **task-specific** WebBridge session name, for example `hermes-bot-combination-research`; pass it on every command in the task.
- One task = one WebBridge session = one tab group. A Perplexity thread is the website conversation and is not the same thing as either the WebBridge session or the Chrome extension connection.
- Before navigating, call `list_tabs` in the chosen session. Do not interpret an empty tab list as a failure: `{success:true,tabs:[]}` means the bridge is healthy and the session has no tab yet.

## Core Workflow

### 0. Connection precheck (verified 2026-09-05)

```bash
curl -s -X POST http://127.0.0.1:10086/command \
  -H 'Content-Type: application/json' \
  -d '{"action":"list_tabs","args":{},"session":"TASK_SESSION"}'
```

Interpret the result before changing anything:

- `success:true` with `tabs:[]` → the Chrome extension handshake is healthy; navigate in this same session.
- `success:true` with existing tabs → reuse the intended tab/session and avoid opening duplicates.
- `no extension connected` → the daemon has no current extension handshake. If the daemon itself is unreachable, start it once with `~/.kimi-webbridge/bin/kimi-webbridge start`, then retry `list_tabs`; if the daemon is reachable but the error persists, ask the user to check the extension connection.
- An extension message such as “already linked” is not a Perplexity thread limit. It usually means an existing daemon/session owns the browser connection or a stale pairing is being reported. Reuse the current task session; never stop/restart/close the user's connection automatically.

A successful `navigate` returning a `tabId`, followed by `list_tabs` showing one active Perplexity tab, is the authoritative connection readback.

### 1. Search (Proven 2026-08-29; connection smoke-tested 2026-09-05)

```bash
# Navigate to Perplexity
curl -s -X POST http://127.0.0.1:10086/command \
  -H 'Content-Type: application/json' \
  -d '{"action":"navigate","args":{"url":"https://www.perplexity.ai","newTab":true,"group_title":"Perplexity Search"},"session":"TASK_SESSION"}'
# Wait 4s for page load

# Snapshot to get @e refs (use compact JSON for regex)
curl -s -X POST http://127.0.0.1:10086/command \
  -H 'Content-Type: application/json' \
  -d '{"action":"snapshot","args":{},"session":"TASK_SESSION"}'
# Textbox: role="textbox", name="问任何事情..." or "输入 @ 以使用连接器"
# Search button: role="button", name="搜索"

# Click textbox FIRST (required to focus it)
curl -s -X POST http://127.0.0.1:10086/command \
  -H 'Content-Type: application/json' \
  -d '{"action":"click","args":{"selector":"@eNN"},"session":"TASK_SESSION"}'

# Fill query
curl -s -X POST http://127.0.0.1:10086/command \
  -H 'Content-Type: application/json' \
  -d '{"action":"fill","args":{"selector":"@eNN","value":"YOUR QUERY"},"session":"TASK_SESSION"}'
# Returns: {"mode":"contenteditable","success":true,"tag":"DIV"}

# Submit via three-event Enter combo (CRITICAL — single keydown doesn't work)
curl -s -X POST http://127.0.0.1:10086/command \
  -H 'Content-Type: application/json' \
  -d '{"action":"evaluate","args":{"code":"(() => { const el = document.querySelector(\"[contenteditable]\"); el.dispatchEvent(new InputEvent(\"beforeinput\",{inputType:\"insertText\",data:\"\\n\",bubbles:true,cancelable:true})); el.dispatchEvent(new KeyboardEvent(\"keydown\",{key:\"Enter\",code:\"Enter\",keyCode:13,which:13,bubbles:true,cancelable:true})); el.dispatchEvent(new KeyboardEvent(\"keyup\",{key:\"Enter\",code:\"Enter\",keyCode:13,which:13,bubbles:true,cancelable:true})); })()"},"session":"TASK_SESSION"}'
```

### 2. Wait & Expand

```bash
# Wait 10-15 seconds for answer generation
sleep 12

# Click "查看更多" to expand full answer
curl -s -X POST http://127.0.0.1:10086/command \
  -H 'Content-Type: application/json' \
  -d '{"action":"evaluate","args":{"code":"(() => { const btns = Array.from(document.querySelectorAll(\"button\")); const more = btns.find(b=>b.innerText.includes(\"查看更多\")); if(more) { more.click(); return \"clicked\"; } return \"no expand button\"; })()"},"session":"TASK_SESSION"}'
```

### 3. Extract Answer

```bash
# Get full answer text from main area
curl -s -X POST http://127.0.0.1:10086/command \
  -H 'Content-Type: application/json' \
  -d '{"action":"evaluate","args":{"code":"(() => { const main = document.querySelector(\"main\"); return main ? JSON.stringify({text:main.innerText, url:location.href, title:document.title}) : \"no main\"; })()"},"session":"TASK_SESSION"}'
```

### 4. Extract Sources

Keep the direct URLs from the page. Record unavailable, paywalled, or contradictory sources explicitly.

The candidate-vs-verified boundary (Perplexity's answer and source labels are candidate output; a claim becomes verified only after canonical page readback) is defined once in Step 3 (Verification) of the `perplexity-search` skill. Apply that definition; do not restate it here. Note that this skill is the low-level browser layer — a `perplexity` CLI/toolkit route also exists; see the `perplexity-conversational-research` skill for when to prefer it.

```bash
# Click "链接" tab
curl -s -X POST http://127.0.0.1:10086/command \
  -H 'Content-Type: application/json' \
  -d '{"action":"evaluate","args":{"code":"(() => { const tabs = Array.from(document.querySelectorAll(\"[role=tab]\")); const linksTab = tabs.find(t=>t.innerText.includes(\"链接\")); if(linksTab) { linksTab.click(); return \"clicked\"; } return \"not found\"; })()"},"session":"TASK_SESSION"}'

# Extract all source links
curl -s -X POST http://127.0.0.1:10086/command \
  -H 'Content-Type: application/json' \
  -d '{"action":"evaluate","args":{"code":"(() => { const main = document.querySelector(\"main\"); const links = Array.from(main.querySelectorAll(\"a[href]\")).map(a=>({text:a.textContent.trim().substring(0,200),href:a.href})).filter(l=>l.href.startsWith(\"http\")&&!l.href.includes(\"perplexity.ai\")&&l.text.length>2); return JSON.stringify(links); })()"},"session":"TASK_SESSION"}'
```

### 5. Follow-up Questions

```bash
# Click a follow-up suggestion (by text content)
curl -s -X POST http://127.0.0.1:10086/command \
  -H 'Content-Type: application/json' \
  -d '{"action":"evaluate","args":{"code":"(() => { const btns = Array.from(document.querySelectorAll(\"button\")); const target = btns.find(b=>b.innerText.includes(\"FOLLOW UP TEXT\")); if(target) { target.click(); return \"clicked\"; } return \"not found\"; })()"},"session":"TASK_SESSION"}'
```

### 6. New Search (Continue in Same Tab)

Use the same task session and current tab. Click the follow-up textbox first, then use `fill`; do not mutate `textContent` directly because React/editor state may not update.

```bash
# Snapshot again if the @e ref may have changed; use the textbox ref or CSS fallback
curl -s -X POST http://127.0.0.1:10086/command \
  -H 'Content-Type: application/json' \
  -d '{"action":"click","args":{"selector":"@eNN"},"session":"TASK_SESSION"}'

curl -s -X POST http://127.0.0.1:10086/command \
  -H 'Content-Type: application/json' \
  -d '{"action":"fill","args":{"selector":"@eNN","value":"NEW QUERY"},"session":"TASK_SESSION"}'

# Submit with the three-event Enter sequence from Step 1.
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
2. Runs a `list_tabs` connection precheck before navigating
3. Searches Perplexity
4. Waits for results
5. Expands the answer
6. Returns `{answer, sources, url, follow_ups}`

The `list_tabs` precheck runs before every navigation: `{success:true, tabs:[]}` is healthy and the run continues; a `no extension connected` response or any non-success result aborts with a connection error instead of blindly navigating.

The CDP `Input.insertText` fallback (used when `fill` fails) applies to the **initial** search only. The follow-up path must not use CDP insertText — keep follow-ups on the click/fill path, consistent with the conversational skill.

## Known Limitations

1. **Answer truncation**: Answers are collapsed by default; must click "查看更多"
2. **Element refs change**: Every session gets different @e refs; use role+name or JS selectors
3. **Model dropdown**: React synthetic events; may need CDP for model switching
4. **Rate limits**: Perplexity Pro has usage limits; batch carefully
5. **No Deep Research automation yet**: Need to map the Deep Research UI flow
6. **Focus modes not mapped**: Academic, Writing, Math modes need exploration
7. **File upload not mapped**: Need to explore the upload flow

## Session Management

- Use one task-specific session name for all Perplexity operations, even across follow-ups and source pages.
- Reuse the current tab for a related follow-up; use `newTab:true` only when pages genuinely need to coexist.
- Do not close the session automatically. Per `kimi-webbridge`, `close_session` is user-initiated only (for example, the user explicitly asks to close or clear the tabs).
- If the extension says it is already linked, inspect `list_tabs` and continue with the existing daemon/session instead of creating a new thread/session.
- Tabs accumulate in the session's group only when deliberately opened with `newTab:true`; keep the group small and readable.
