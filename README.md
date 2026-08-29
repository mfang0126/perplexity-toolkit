# Perplexity Skill — 浏览器自动化 + 问题全景

## 目标

榨干 Perplexity AI：通过浏览器自动化完整控制 Perplexity 搜索流程，同时建立已知问题全景数据库。

## 项目结构

```
perplexity-skill/
├── src/                          # 代码
│   └── perplexity_search.py      # 核心搜索脚本（已验证）
├── docs/                         # 开发文档
│   ├── SKILL.md                  # Hermes Skill 定义
│   ├── perplexity-browser-mapping.md   # 浏览器交互测绘
│   ├── perplexity-reddit-issues.md     # Reddit 问题汇总
│   ├── perplexity-twitter-community-issues.md  # X/社区问题
│   └── perplexity-capability-analysis.md       # Web vs API 能力差距
├── tests/                        # 测试
├── scripts/                      # 辅助脚本
└── README.md
```

## 已验证的自动化流程

1. **navigate** → perplexity.ai
2. **snapshot** → 获取 textbox @e ref
3. **click** textbox → 聚焦（必须先点击）
4. **fill** textbox → 写入查询
5. **三事件 Enter** → beforeinput + keydown + keyup
6. **wait** 10-15s → 等待答案生成
7. **"查看更多"** → 展开折叠答案
8. **extract** → 提取答案文本 + 源链接 + 后续问题

## 关键发现

| 问题 | 解决方案 |
|------|---------|
| fill 不生效 | 必须先 click textbox 再 fill |
| Enter 不触发搜索 | 需要 beforeinput + keydown + keyup 三事件 |
| 答案被截断 | 点击"查看更多"按钮展开 |
| @e ref 每次不同 | 用 snapshot + regex 动态查找 |
| snapshot JSON 有空格 | 用 `separators=(",",":")` 压缩 |

## 核心研究发现

### API vs Web 差距（最重要）
- Web UI 和 API 是**故意设计成不同产品**
- 官方明确表示**不打算让 API 输出质量匹配 UI**
- Web-only 功能：Pro Search、Collections/Spaces、Focus 模式、图片生成、Model Council、400+ 连接器
- 这是浏览器自动化的核心价值

### 已知问题 Top 5
1. **搜索幻觉** — 37% 引用错误率（8 个 AI 搜索工具中最好，但仍超 1/3）
2. **长文/代码/推理弱** — 不如原生 GPT/Claude/Gemini 应用
3. **记忆薄/线程断** — 不是可搜索的知识库
4. **配额不透明** — Pro/Max 限制是"平均使用"而非硬数字
5. **搜索绑定** — 不是完整工作区

## 下一步

- [ ] Deep Research 模式自动化
- [ ] Focus 模式切换（Academic/Writing/Math）
- [ ] 批量搜索管线
- [ ] 结果聚合 + 去重
- [ ] 文件上传流程
- [ ] 模型切换自动化
