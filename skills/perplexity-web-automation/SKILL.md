---
name: perplexity-web-automation
description: |
  Use when the user asks to search Perplexity through a real browser. Automate Perplexity via Kimi WebBridge, extract answers and sources, and preserve task-level session state.
metadata:
  version: "0.1.3"
  requires: ["kimi-webbridge", "webbridge-hygiene"]
---

# Perplexity Web Automation Skill

Automate search and extraction from Perplexity AI (Pro account) via Kimi WebBridge browser control.

## Route boundary

This is the direct-browser execution layer, not the default Perplexity route.
Use it when the user explicitly requests a web page, browser, Chrome, WebBridge,
Kimi WebBridge, opening Perplexity, or continuing the current browser thread.
For an ordinary Perplexity request without that wording, the high-level policy
uses the `perplexity` CLI first. Do not invoke the CLI first when this direct
browser route is explicitly selected.

The global `browser-routing` skill remains the browser decision layer. For a
Perplexity direct-browser request it selects `kimi-webbridge`; load
`webbridge-hygiene` with it. This skill is the Perplexity-specific domain
adapter on top of that generic browser driver.

## Named-model verification

A workflow mode is not a named model. If the user asks for K3 or another
specific model, select it through the visible Perplexity model control and read
back the label after selection. Return `selected_model` and
`model_label_verified: true` only when the label is actually visible; otherwise
stop with an unverified result and do not infer the model from answer style.

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

### 4a. Candidate-selection evidence (mandatory for “which is better?”)

When Perplexity is used to compare, recommend, shortlist, or assess maturity/adoption, do **not** ask only for features and links. Put the evidence fields in the Perplexity prompt and extract them separately:

```text
For every candidate, provide direct URLs and an as-of date for:
- GitHub stars/forks and a second adoption signal (package downloads, dependents, registry installs, or public production references)
- latest release and latest commit; contributor concentration and a small issue/PR responsiveness sample
- two independent community discussions: date, net votes/replies, author affiliation, and concrete success/failure evidence
- security/permission model and whether a secret can enter LLM context
- exact unknowns. Do not convert missing data into a ranking.
```

Perplexity's metrics and source labels are discovery leads, not verified facts. For finalists, read the canonical repository/registry/official document, record the metric capture date, and preserve `ui_source_count`, `unique_dom_urls`, and canonical-readback status separately. Do not report “popular”, “active”, “many users”, or “best” without the raw indicator and its scope.

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

## Resident console (recommended for repeat / multi-turn use)

For a fixed group+tab workflow, prefer the toolkit's resident console over hand-driving the raw steps:

```bash
perplexity console ask "query" [--task NAME] [--new-thread] [-f json]   # one task = one thread
perplexity console status                                              # state + live readback
perplexity console threads                                             # recorded task threads
perplexity console selfcheck                                           # canned full-gate run
```

Conventions: WebBridge session `perplexity-console`, group «Perplexity 控制台», exactly one tab. Durable state lives in `~/.perplexity-console/state.json` (session, group, per-task thread URL) because session→tab mappings are daemon-memory only and die on daemon restart — the console attach-or-recreates by reopening the saved thread URL. Every step carries a readback gate; failures raise with a screenshot under `~/.perplexity-console/evidence/`.

Live-verified UI behaviors (2026-09; bake these into any direct-browser flow):

- The composer is a controlled React editor. `fill` (clear-and-insert) is its ONLY reliable mutation path; CDP key events, execCommand and DOM/range edits get reverted by re-render, and empty/whitespace fill values are silent no-ops.
- `fill` can also silently no-op on a fresh/unfocused editor while returning `success: true` — always read the composer back.
- The submit button is `button[aria-label="提交"]`; its `disabled` flag mirrors the editor's internal state (disabled = state empty even if the DOM shows text). Verify `enabled` before clicking; a DOM/state desync (DOM shows text, button disabled) is healed by one page reload.
- A late async draft-restore can merge old draft text into the composer AFTER a successful fill; the submission then carries draft+query (seen live). Re-verify composer equality immediately before submitting; repair by re-filling (fill replaces).
- Per-turn scoping (never use `main.innerText` in a thread): user turns = `[class*="user-bubble"]` filtered `:not(.opacity-0)` (text = query + "\nHH:MM"); completion marker = one `已研究` pill per answered turn (count increments); the answer body = the LAST `main div.prose` (one per turn). The expand control in the new UI is a button labeled 「展开」 (the legacy 「查看更多」 did not appear in live mapping).

Model selector & attachments (added 2026-09-21):

- `perplexity console models` lists the selector menu (name/badges/checked; submenu entries like "GPT-5.6 Sol | Max" are flagged and not programmatic-selectable yet); `perplexity console model "<name>"` switches with a verified readback of the button's aria-label. The menu is a Radix portal: open and select ONLY with trusted CDP mouse clicks at element coordinates (synthetic clicks do nothing); close leftovers with Escape via CDP.
- `ask --file PATH` (repeatable) attaches local files by building them in-page (base64 → Uint8Array → File → DataTransfer → input change event). This deliberately bypasses the WebBridge `upload` action, which requires Chrome's per-extension "Allow access to file URLs" (off by default, not toggleable by the extension; CDP `DOM.setFileInputFiles` is also blocked with "Not allowed"). Keep injection for files ≤8MB; for larger files point the user to the chrome://extensions toggle. Attachment chips verify via `aria-label="移除 <name>"`; wait ≥2s after chips appear before touching the composer.
- Send hardening: an attachment chip keeps the submit button enabled even while the TEXT state lags — observed live as a FILE-ONLY submission. The pipeline now re-fills right before submit (freshness pass), re-verifies user-turn ownership afterwards, and recovers from a misfire with one reload + file re-inject + bounded retry (guarded by a delayed-ownership recheck so a slow-but-correct turn is never sent twice). The completion gate requires the turn-scoped prose count to GROW past the pre-submit baseline before stability counts — "the last answer hasn't changed" alone is not completion (a slow file-bearing answer once let that pass).

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

1. **Answer truncation**: long answers arrive collapsed behind a fade mask; click the 「展开」 control (new UI; legacy label "查看更多") — and scope extraction to the LAST `div.prose` so earlier thread turns never leak into the answer
2. **Element refs change**: Every session gets different @e refs; use role+name or JS selectors
3. **Model dropdown**: React synthetic events; may need CDP for model switching
4. **Rate limits**: Perplexity Pro has usage limits; batch carefully
5. **No Deep Research automation yet**: Need to map the Deep Research UI flow
6. **Focus modes not mapped**: Academic, Writing, Math modes need exploration
7. **File upload not mapped**: Need to explore the upload flow
8. **Fill is the only reliable editor mutation path** — keyboard/CDP/DOM edits are reverted by the controlled re-render, empty fills are no-ops, and a fill can silently no-op while reporting success. Read the composer back and require equality.
9. **Draft-restore merge**: a late async draft restore can merge leftover draft text into a successfully-filled composer; the submitted message then contains draft+query. Re-verify the composer immediately before submit and re-fill on mismatch.
10. **DOM/state desync**: the editor's visible text can disagree with its internal state (submit button disabled while the DOM shows text). Use the submit button's `disabled` flag as the state signal, and heal with a single page reload.
11. **Thread extraction scoping**: in a multi-turn thread `main.innerText` contains the whole conversation; extract only the last `div.prose` and identify turns via `user-bubble` + `已研究` counts.

## Session Management

- Use one task-specific session name for all Perplexity operations, even across follow-ups and source pages.
- Reuse the current tab for a related follow-up; use `newTab:true` only when pages genuinely need to coexist.
- Do not close the session automatically. Per `kimi-webbridge`, `close_session` is user-initiated only (for example, the user explicitly asks to close or clear the tabs).
- If the extension says it is already linked, inspect `list_tabs` and continue with the existing daemon/session instead of creating a new thread/session.
- Tabs accumulate in the session's group only when deliberately opened with `newTab:true`; keep the group small and readable.
