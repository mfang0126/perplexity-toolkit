# Perplexity Toolkit

Automate Perplexity AI search via browser control — search, extract, batch, and analyze.

## What It Does

- **4 search modes**: Standard, Deep Research, Model Council, Step-by-step Learning
- **Batch pipeline**: Search hundreds of queries with resume and rate limiting
- **Result aggregation**: Dedup sources, rank by frequency, generate reports
- **Source extraction**: Get all cited URLs with titles and snippets
- **Follow-up capture**: Extract Perplexity's suggested follow-up questions

## Quick Start

```bash
# Install
pip install -e .

# Single search
perplexity search "best AI coding agents 2026"

# Deep Research (multi-step, 60-120s)
perplexity search "AI safety risks 2026" -m deep_research

# Batch from file
perplexity batch -i queries.json -o results.json

# Aggregate results
perplexity aggregate results.json -f markdown
```

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

To add a new backend (Playwright, Selenium, etc.), implement `BrowserDriver` in `drivers/`:

```python
from perplexity_toolkit.drivers.base import BrowserDriver

class PlaywrightDriver(BrowserDriver):
    def navigate(self, url, new_tab=True, group_title=""): ...
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

## 快速开始

```bash
# 安装
pip install -e .

# 单次搜索
perplexity search "2026 年最好的 AI 编程助手"

# 深度研究（多步推理，约 60–120 秒）
perplexity search "2026 年 AI 安全风险" -m deep_research

# 从文件批量搜索
perplexity batch -i queries.json -o results.json

# 聚合结果并生成报告
perplexity aggregate results.json -f markdown

# 查看搜索历史
perplexity history
```

## CLI 命令一览

| 命令 | 说明 |
| --- | --- |
| `perplexity search` | 单次搜索（4 种模式可选） |
| `perplexity batch` | 批量搜索，支持恢复与限速 |
| `perplexity aggregate` | 聚合结果、去重来源、生成报告 |
| `perplexity history` | 管理搜索历史 |

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

接入新后端（Playwright、Selenium 等）时，在 `drivers/` 下实现 `BrowserDriver`：

```python
from perplexity_toolkit.drivers.base import BrowserDriver

class PlaywrightDriver(BrowserDriver):
    def navigate(self, url, new_tab=True, group_title=""): ...
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

## 已知限制

- 深度研究模式会在查询中多出一个 "/" 前缀（Perplexity 可正常处理）
- 模型选择下拉框需要 CDP 级点击（尚未自动化）
- 文件上传流程尚未映射

## 许可

MIT
