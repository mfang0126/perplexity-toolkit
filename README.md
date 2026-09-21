# Perplexity Toolkit

Automate Perplexity AI search via browser control — search, extract, batch, and analyze.

## What It Does

- **4 search modes**: Standard, Deep Research, Model Council, Step-by-step Learning
- **Batch pipeline**: Search hundreds of queries with resume and rate limiting
- **Result aggregation**: Dedup sources, rank by frequency, generate reports
- **Source extraction**: Get all cited URLs with titles and snippets
- **Follow-up capture**: Extract Perplexity's suggested follow-up questions
- **Agent skills**: Three ready-to-load `SKILL.md` files in [`skills/`](skills/) — routing policy, session discipline, and the verification rule — for agents that drive this toolkit

## Quick Start

```bash
# Install so `perplexity` is on your PATH (recommended)
pipx install git+https://github.com/mfang0126/perplexity-toolkit.git

# ...or for development, into the CURRENT environment only:
#   pip install -e .
# If you use pip, install into the same environment your agent runs,
# otherwise `command -v perplexity` will not find it.

# Single search
perplexity search "best AI coding agents 2026"

# Keep several CLI commands in one task session; this global option comes
# before the subcommand.
perplexity --session-prefix coding-agents-2026 search "best AI coding agents 2026"

# Skip the default quality/readback annotation only when explicitly needed.
perplexity --no-verify search "best AI coding agents 2026"

# Classify the original user wording before selecting CLI or direct browser.
# This command is local; it does not open Chrome or call Perplexity.
perplexity route -f json "Please use Perplexity in Chrome"

# Deep Research (multi-step, 60-120s)
perplexity search "AI safety risks 2026" -m deep_research

# Batch from file
perplexity batch -i queries.json -o results.json

# Aggregate results
perplexity aggregate results.json -f markdown
```

Search output is quality-annotated by default. A JSON search result is always
one JSON document on stdout; human-readable quality text is not appended to
`-f json`. The quality block distinguishes HTTP reachability from bounded page
readback and reports `verification_state: candidate` / `claim_support:
not_evaluated` until a human or semantic evidence pass confirms each claim.

## Resident Console (fixed tab/group)

Keep one persistent tab/group for Perplexity and route every question through verified steps — one task = one thread:

```bash
perplexity console ask "Will X happen?" --task my-project -f json   # creates or continues the task thread
perplexity console ask "And the Y angle?" --task my-project          # follow-up in the same thread
perplexity console ask "New topic" --new-thread --task other         # fresh thread in the same tab
perplexity console ask "Review this" --file report.pdf               # attach local files (repeatable, <=8MB)
perplexity console models                                            # list selectable models
perplexity console model "Claude Sonnet 5"                           # switch model (verified readback)
perplexity console status      # state + live tab readback
perplexity console threads     # recorded task threads
perplexity console selfcheck   # run the full gate pipeline on a canned query
```

Granular steps for intent-driven composition (share one implementation with `ask`, joined by a staged-turn ledger):

```bash
perplexity console fill "q" [--task T] [--file F]   # stage: attach + fill + verify (no send)
perplexity console submit                            # submit the staged turn (verified, self-healing)
perplexity console wait [--wait N]                   # wait for the staged answer to settle
perplexity console extract                           # turn-scoped answer; consumes the staged turn
perplexity console send "q"                          # fill + submit only
perplexity console attach --file F | files | detach NAME
perplexity console open <task|url> [--new-thread]
```

- **Gates, not best effort**: every step is verified (composer equality incl. editor state, user-turn ownership, completion signals, turn-scoped answer extraction). Failures raise with a screenshot under `~/.perplexity-console/evidence/` and a machine-readable `error_code` — silent wrong answers are the one outcome the console never reports as success.
- **Stepwise, not one-shot**: `ask` is a composite of granular steps (`fill → submit → wait → extract`) that share one implementation each; the steps are also callable alone and compose through a staged-turn ledger (`state.json → pending`), so retries and unusual flows are per-step instead of all-or-nothing. Every run appends one line to `~/.perplexity-console/runs.jsonl`.
- **Model & files**: `perplexity console models` / `model "<name>"` switch the Perplexity model with a verified readback; `ask --file` attaches local files (in-page injection, ≤8MB) and verifies every attachment chip before sending.
- **Durable state**: `~/.perplexity-console/state.json` (session, group, per-task thread URLs). WebBridge session→tab mappings are daemon-memory only; the console attach-or-recreates the tab from the saved thread URL after daemon/browser restarts.
- **Self-heal**: a desynced editor (DOM text vs internal state) is repaired by one bounded page reload before failing loudly; mis-sent turns (e.g. file-only) recover via reload + re-inject + a bounded, duplicate-safe retry.

## Python API

```python
from perplexity_toolkit.search import search, deep_research, model_council

# Standard search
result = search("Python vs Rust 2026")
print(result["answer"])      # Full answer text
print(result["sources"])     # [{text, href}, ...]
print(result["follow_ups"])  # ["follow-up question", ...]

# Deep Research (longer, more detailed)
result = deep_research("AI agent frameworks comparison")

# Model Council (multiple models answer)
result = model_council("best programming language for beginners")
```

## Batch Pipeline

```python
from perplexity_toolkit.batch import run_batch

queries = [
    {"query": "topic 1", "mode": "search"},
    {"query": "topic 2", "mode": "deep_research"},
]
results = run_batch(queries, output_file="results.json", delay=5.0)
```

## Architecture

```
perplexity_toolkit/
├── __init__.py          # Package init
├── config.py            # Configuration management
├── search.py            # Core search functions (4 modes)
├── batch.py             # Batch pipeline with resume
├── aggregator.py        # Result aggregation + reports
├── drivers/             # Browser driver abstraction
│   ├── base.py          # Abstract BrowserDriver interface
│   └── webbridge.py     # Kimi WebBridge implementation
├── utils/               # DOM parsing + event helpers
│   └── __init__.py
└── commands/            # CLI
    └── cli.py
```

## Browser Driver

The toolkit uses an abstract `BrowserDriver` interface. Current implementation:

- **WebBridgeDriver** — Kimi WebBridge (Chrome extension + local daemon)

This is the **only** shipped backend. Every search mode drives a real logged-in
browser through it; there is no API-key or headless path. `aggregate` is the one
subcommand that runs without a browser, because it only post-processes result
JSON you already fetched.

To add a new backend (Playwright, Selenium, etc.), implement `BrowserDriver` in `drivers/`:

```python
from perplexity_toolkit.drivers.base import BrowserDriver

class PlaywrightDriver(BrowserDriver):
    def navigate(self, url, new_tab=True, group_title=""): ...
    # Optional session hygiene hook; unsupported drivers may omit it.
    def list_tabs(self): ...
    def snapshot(self): ...
    def click(self, selector): ...
    def fill(self, selector, value): ...
    def evaluate(self, code): ...
    def screenshot(self, path=None): ...
    def close(self): ...
```

## Requirements

- Python 3.9+
- Kimi WebBridge daemon (`~/.kimi-webbridge/bin/kimi-webbridge start`)
- Chrome with Kimi WebBridge extension installed
- Perplexity account (free or Pro)

## Verify Your Setup

Run these three checks in order. Each one isolates a different failure.

```bash
# 1. Is the command reachable?
command -v perplexity && perplexity --help >/dev/null && echo "CLI OK"

# 2. Is the WebBridge daemon up?
curl -s -X POST http://127.0.0.1:10086/command \
  -H 'Content-Type: application/json' \
  -d '{"action":"list_tabs"}'

# 3. Is Chrome connected?
#    Step 2 returns {"ok":true,...} when the daemon is up.
#    If it reports "no extension connected", the daemon is running but Chrome
#    is not attached — open Chrome and check the WebBridge extension.
```

| Symptom | Cause | Fix |
|---|---|---|
| `command -v perplexity` finds nothing | Installed into an environment that is not on your `PATH` | `pipx install git+https://github.com/mfang0126/perplexity-toolkit.git`, or export `PERPLEXITY_BIN=/full/path/to/perplexity` |
| `curl` to port 10086 fails | Daemon not started | `~/.kimi-webbridge/bin/kimi-webbridge start` |
| Daemon replies `no extension connected` | Chrome not attached | Open Chrome; confirm the WebBridge extension is enabled. This is **not** a rate limit or a thread cap |
| Search returns an empty answer | Not logged in to Perplexity in that browser | Log in to perplexity.ai in the same Chrome profile |

Note for agent authors: do not probe with a bare `python3 -c "import perplexity_toolkit"`.
The toolkit lives in whichever environment it was installed into, which is usually
not the interpreter that `python3` resolves to. Probe for the console script
(`command -v perplexity`, `$PERPLEXITY_BIN`, `$VIRTUAL_ENV/bin/perplexity`) instead,
and report "not resolved in this environment" rather than "not installed".

## Known Limitations

- Deep Research mode leaks a "/" prefix in the query (Perplexity handles it gracefully)
- Model selector dropdown requires CDP-level clicks (not yet automated)
- File upload flow not yet mapped

## Research

See `docs/research/` for comprehensive analysis of Perplexity's known issues, API vs web gap, and browser automation mapping.

## License

MIT

---

# 中文说明 (Chinese)

## 简介

Perplexity Toolkit 通过浏览器控制自动化 Perplexity AI 搜索 — 支持搜索、提取、批量处理和结果分析。

## 核心功能

- **4 种搜索模式**：标准搜索（Standard）、深度研究（Deep Research）、模型委员会（Model Council）、逐步学习（Step-by-step Learning）
- **批量流水线**：批量搜索数百条查询，支持断点续跑与速率限制
- **结果聚合**：去重来源、按频次排序、自动生成报告
- **来源提取**：获取所有引用链接，含标题与摘要
- **追问捕获**：提取 Perplexity 推荐的后续追问问题
- **历史管理**：查看与管理搜索历史
- **Agent Skills**：[`skills/`](skills/) 下三份可直接加载的 `SKILL.md`——路由策略、会话纪律、验证规则——供驱动本工具包的 agent 使用

## 快速开始

```bash
# 安装（推荐）：让 `perplexity` 进入 PATH
pipx install git+https://github.com/mfang0126/perplexity-toolkit.git

# 或开发模式，只装进「当前」环境：
#   pip install -e .
# 用 pip 时务必装进 agent 实际运行的那个环境，
# 否则 `command -v perplexity` 找不到它。

# 单次搜索
perplexity search "2026 年最好的 AI 编程助手"

# 多条 CLI 命令共用同一个任务 session；全局选项必须放在子命令前
perplexity --session-prefix coding-agents-2026 search "2026 年最好的 AI 编程助手"

# 仅在明确需要时跳过默认质量/readback 标注
perplexity --no-verify search "2026 年最好的 AI 编程助手"

# 深度研究（多步推理，约 60–120 秒）
perplexity search "2026 年 AI 安全风险" -m deep_research

# 从文件批量搜索
perplexity batch -i queries.json -o results.json

# 聚合结果并生成报告
perplexity aggregate results.json -f markdown

# 查看搜索历史
perplexity history
```

搜索默认会附加质量检查。JSON 输出始终是 stdout 上的单个 JSON 文档，
不会再混入人类可读的质量文本。质量结果区分 HTTP 可达性和有界页面
readback，并在逐条 claim 经过人工或语义证据核验前标记为
`verification_state: candidate`、`claim_support: not_evaluated`。

## CLI 命令一览

| 命令 | 说明 |
| --- | --- |
| `perplexity search` | 单次搜索（4 种模式可选） |
| `perplexity batch` | 批量搜索，支持恢复与限速 |
| `perplexity aggregate` | 聚合结果、去重来源、生成报告 |
| `perplexity history` | 管理搜索历史 |

## 常驻控制台（固定 tab / 固定 group）

锁定一个 tab 一个 group 专门给 Perplexity，每个任务 = 一个线程：

```bash
perplexity console ask "问题" --task my-project -f json    # 创建或续接任务线程
perplexity console ask "追问" --task my-project            # 同一线程内追问
perplexity console ask "新话题" --new-thread --task other  # 同一 tab 内开新线程
perplexity console status | threads | selfcheck
```

- 每步都有读回闸门（输入框等值+编辑器状态、提问轮次归属、完成信号、轮次作用域提取）；失败必带截图证据（`~/.perplexity-console/evidence/`），不会把未验证结果当作成功。
- 状态落盘 `~/.perplexity-console/state.json`；daemon 重启后自动重建 tab 并回到保存的线程 URL（session→tab 映射仅存于 daemon 内存）。
- 编辑器与内部状态脱钩时先做一次有界重载自愈，仍失败则大声报错。

## Python API

```python
from perplexity_toolkit.search import search, deep_research, model_council

# 标准搜索
result = search("Python vs Rust 2026")
print(result["answer"])      # 完整回答文本
print(result["sources"])     # [{text, href}, ...]
print(result["follow_ups"])  # ["追问问题", ...]

# 深度研究（更详细、耗时更长）
result = deep_research("AI agent 框架对比")

# 模型委员会（多模型多角度回答）
result = model_council("新手最适合学什么编程语言")
```

## 批次流水线

```python
from perplexity_toolkit.batch import run_batch

queries = [
    {"query": "主题 1", "mode": "search"},
    {"query": "主题 2", "mode": "deep_research"},
]
results = run_batch(queries, output_file="results.json", delay=5.0)
```

## 架构

```
perplexity_toolkit/
├── __init__.py          # 包初始化
├── config.py            # 配置管理
├── search.py            # 核心搜索函数（4 种模式）
├── batch.py             # 批量流水线（支持恢复）
├── aggregator.py        # 结果聚合与报告
├── drivers/             # 浏览器驱动抽象层
│   ├── base.py          # 抽象 BrowserDriver 接口
│   └── webbridge.py     # Kimi WebBridge 实现
├── utils/               # DOM 解析与事件辅助
│   └── __init__.py
└── commands/            # CLI
    └── cli.py
```

## 浏览器驱动

工具包基于抽象 `BrowserDriver` 接口。当前实现：

- **WebBridgeDriver** — Kimi WebBridge（Chrome 扩展 + 本地守护进程）

这是**唯一**已实现的后端。所有搜索模式都通过它驱动真实的已登录浏览器，
没有 API key 或无头模式路径。`aggregate` 是唯一不需要浏览器的子命令，
因为它只对你已经抓取到的结果 JSON 做后处理。

接入新后端（Playwright、Selenium 等）时，在 `drivers/` 下实现 `BrowserDriver`：

```python
from perplexity_toolkit.drivers.base import BrowserDriver

class PlaywrightDriver(BrowserDriver):
    def navigate(self, url, new_tab=True, group_title=""): ...
    # Optional session hygiene hook; unsupported drivers may omit it.
    def list_tabs(self): ...
    def snapshot(self): ...
    def click(self, selector): ...
    def fill(self, selector, value): ...
    def evaluate(self, code): ...
    def screenshot(self, path=None): ...
    def close(self): ...
```

## 环境要求

- Python 3.9+
- Kimi WebBridge 守护进程（`~/.kimi-webbridge/bin/kimi-webbridge start`）
- 已安装 Kimi WebBridge 扩展的 Chrome 浏览器
- Perplexity 账号（免费版或 Pro 均可）

## 安装后自检

按顺序跑这三步，每一步隔离一类故障。

```bash
# 1. 命令是否可达？
command -v perplexity && perplexity --help >/dev/null && echo "CLI OK"

# 2. WebBridge 守护进程是否启动？
curl -s -X POST http://127.0.0.1:10086/command \
  -H 'Content-Type: application/json' \
  -d '{"action":"list_tabs"}'

# 3. Chrome 是否已连接？
#    守护进程正常时第 2 步返回 {"ok":true,...}。
#    若返回 "no extension connected"，说明进程在跑但 Chrome 没接上——
#    打开 Chrome 并检查 WebBridge 扩展。
```

| 现象 | 原因 | 处理 |
|---|---|---|
| `command -v perplexity` 找不到 | 装进了不在 `PATH` 上的环境 | `pipx install git+https://github.com/mfang0126/perplexity-toolkit.git`，或 `export PERPLEXITY_BIN=/完整/路径/perplexity` |
| curl 连 10086 失败 | 守护进程没启动 | `~/.kimi-webbridge/bin/kimi-webbridge start` |
| 守护进程返回 `no extension connected` | Chrome 未接入 | 打开 Chrome，确认 WebBridge 扩展已启用。这**不是**限流或会话数上限 |
| 搜索返回空答案 | 该浏览器未登录 Perplexity | 在同一个 Chrome profile 登录 perplexity.ai |

给 agent 作者的提醒：不要用裸的 `python3 -c "import perplexity_toolkit"` 做探测。
工具包只存在于安装它的那个环境里，通常不是 `python3` 解析到的解释器。
应改为探测可执行文件（`command -v perplexity`、`$PERPLEXITY_BIN`、
`$VIRTUAL_ENV/bin/perplexity`），并把结果报告为「当前环境未解析到」而不是「未安装」。

## 已知限制

- 深度研究模式会在查询中多出一个 "/" 前缀（Perplexity 可正常处理）
- 模型选择下拉框需要 CDP 级点击（尚未自动化）
- 文件上传流程尚未映射

## 许可

MIT
