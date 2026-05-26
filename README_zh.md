# IPCC References Toolkit (中文文档)

端到端研究流水线：从 IPCC 大型报告 PDF 出发，经参考文献提取、Web of
Science 元数据补全、开放获取（OA）全文下载、Markdown 转换、基于
schema 的 LLM 结构化抽取，最终生成可用于发表的 HTML 文献计量分析报告。

> English version: see [`README.md`](./README.md)

---

## 目录

1. [项目简介](#1-项目简介)
2. [流水线架构](#2-流水线架构)
3. [第 1–3 阶段：PDF → 参考文献 → WoS → 合并](#3-第-13-阶段pdf--参考文献--wos--合并)
4. [第 4–7 阶段：PDF 获取 → Markdown → LLM 抽取 → 分析](#4-第-47-阶段pdf-获取--markdown--llm-抽取--分析)
5. [安装](#5-安装)
6. [快速上手](#6-快速上手)
7. [API 配置](#7-api-配置)
8. [输出文件说明](#8-输出文件说明)
9. [法律与伦理说明](#9-法律与伦理说明)
10. [已知限制](#10-已知限制)
11. [开发路线图](#11-开发路线图)
12. [作者与许可](#12-作者与许可)

---

## 1. 项目简介

IPCC 评估报告横跨三个 Working Group、数十个章节，引用数万篇文献。
现有文献计量工具只能停留在元数据层面（引用数、合作网络等），
诸如 *"AR6 WG2 引用的论文中有多少实际采用了 MRIO 方法 + EORA 数据？"*、
*"气候适应文献的政策框架在 AR5 与 AR6 之间发生了什么样的变化？"*
这类问题需要读全文，在万篇尺度下人工不可行。

本工具链将完整流程自动化：

```
   IPCC PDF
      │
      ▼
   [1] 提取参考文献              ──►  references.xlsx, wos_queries.txt
      │
      ▼
   [2] WoS 查询（API 或浏览器）  ──►  wos_exports/
      │
      ▼
   [3] 合并                      ──►  Record.csv, Unrecord.csv
      │
      ▼
   [4] OA 全文 PDF 获取          ──►  pdfs/, pdf_index.csv
      │
      ▼
   [5] PDF → Markdown            ──►  markdown/, markdown_index.csv
      │
      ▼
   [6] LLM 结构化抽取            ──►  extracted/*.json
      │
      ▼
   [7] 文献计量分析              ──►  analysis_report.html, .xlsx, figures/
```

第 1–3 阶段为本地 / API；第 4 阶段**仅使用 OA / 预印本来源**
（Unpaywall、Crossref、OpenAlex、arXiv）——不绕过 paywall；第 5
阶段以 Microsoft MarkItDown 为主，附带降级方案；第 6 阶段调用
Claude API；第 7 阶段产出一份自包含的 HTML 报告。

**适用对象：** 从事 IPCC 相关分析、消费侧排放核算、气候适应文献综述
等研究的学者、博士生与文献计量学研究者；以及任何需要从大型 PDF 报告
出发构建结构化、WoS 索引、LLM 增强语料的项目。

---

## 2. 流水线架构

| 阶段 | 输入 | 工具 | 输出 | 需要 API？ |
|----:|-----|------|------|:--------:|
| 1 | IPCC PDF | PyMuPDF + Crossref | `references.xlsx`, `wos_queries.txt` | 否（Crossref 免费） |
| 2 | `wos_queries.txt` | WoS Starter API 或 Playwright | `wos_*.xlsx` / `wos_*.json` | 推荐 WoS API |
| 3 | `references.xlsx` + WoS 导出 | 合并器 | `Record.csv`, `Unrecord.csv` | 否 |
| 4 | `Record.csv` | Unpaywall + Crossref + OpenAlex + arXiv | `pdfs/`, `pdf_index.csv` | 否（免费 API） |
| 5 | `pdfs/` | MarkItDown → pymupdf4llm → pymupdf | `markdown/`, `markdown_index.csv` | 否 |
| 6 | `markdown/` | Claude API（默认 Sonnet 4.6） | `extracted/*.json` | **需 Anthropic key** |
| 7 | `Record.csv` + `extracted/` | pandas + matplotlib | `analysis_report.html`, `analysis_tables.xlsx` | 否 |

**所有 7 个阶段共同的架构保证：**

- **逐项失败隔离。** 单个 PDF 损坏、下载失败、LLM 输出不可解析，绝不
  会中断整个运行。失败被捕获、附完整 traceback 记录、继续处理下一项。
- **Never-raise 契约。** 每个阶段的入口函数（`run_extraction`、
  `run_acquire_pdfs`、`run_llm_extract` …）合约性地保证返回
  `TaskReport`，而不是抛异常——即使发生致命错误也是如此。
  这让流水线可脚本化、可安全嵌入任何调度框架。
- **可断点续跑。** 第 4–6 阶段维护磁盘索引，重跑会自动跳过已完成的工作。
  通过 `--no-resume` 强制清洁重跑。
- **结构化报告。** 每次运行产出 `task_report_*.txt`，含状态、耗时、
  成功率、输出文件、逐项失败明细。

---

## 3. 第 1–3 阶段：PDF → 参考文献 → WoS → 合并

实现于 [`ipcc_refs_gui.py`](./ipcc_refs_gui.py)，提供 4 个 Tab 的
Tkinter 桌面 GUI。启动：

```bash
python ipcc_refs_gui.py
```

### Tab 1 — 参考文献提取

1. 选 IPCC PDF。
2. 填 `Report`（如 `AR6`）、`Working Group`（如 `WG2`）、邮箱（仅用于
   Crossref polite pool 的 User-Agent，不会外传）。
3. 点 **Run**。进度条 + 实时日志。
4. 完成后弹出结构化摘要。输出：`references.xlsx`、`wos_queries.txt`、
   `crossref_cache.json`；如有章节失败，还会输出
   `failed_chapters.csv` 和 `extraction_errors.log`。

### Tab 2 — WOS 查询

工具链支持两种路径，由**红色高亮**的 WOS API key 字段控制（红色是
故意的，因为 API 路径强烈优先）：

- **API 路径**（把 Starter key 粘进红框）：直接 HTTPS 调用
  `api.clarivate.com/apis/wos-starter`。又快、合规、可脚本化、可无人值守。
- **浏览器路径**（把红框留空）：Playwright 驱动一个持久化 profile 的
  Chromium 窗口。你通过机构 SSO 手动登录一次，然后工具链遍历每个 batch、
  下载 Excel。要求电脑在整个运行期间保持开机。

### Tab 3 — 合并

将 `references.xlsx` 与 WoS 导出文件夹合并，产出：

- **`Record.csv`** —— WoS 完整 schema（约 70 列），每行对应一条在
  WoS 中匹配到的参考文献，按归一化 DOI 去重。
- **`Unrecord.csv`** —— 精简的 8 列 schema（Report、WG、Chapter、
  Chapter title、Authors、Article Title、Publisher、Year），收纳
  在 WoS 中未匹配到的参考文献。

可重复运行：往 WoS 导出文件夹里继续放新文件，再次点 Run 即可。

---

## 4. 第 4–7 阶段：PDF 获取 → Markdown → LLM 抽取 → 分析

实现于 [`pipeline_extras.py`](./pipeline_extras.py)，提供 4 个子命令
+ 一个 `all` 一键命令的 CLI 脚本。这几个阶段以 CLI 为主，是因为它们
通常是长时间运行的批处理任务，更适合放到服务器、`nohup` 后台、
或 CI 中跑。

### 第 4 阶段 — `acquire`：下载开放获取 PDF

对 `Record.csv` 中每条 DOI，按优先级查询四个免费来源，下载第一个能
拿到的 PDF：

1. **Unpaywall API** —— 专门做 OA 索引；需要你的邮箱。
2. **Crossref `link` 字段** —— 出版商标记为 text-mining 友好的直链。
3. **OpenAlex** —— 多来源聚合的 OA location。
4. **arXiv** —— 预印本（对 IPCC 引用文献覆盖度可观）。

每个下载文件都通过 magic bytes（`%PDF`）校验，避免把 HTML 错误页
当作 PDF 收下。单文件上限 100 MB（安全阀）。

```bash
python pipeline_extras.py acquire \
    --records output/Record.csv \
    --out output/stage4_pdfs \
    --email you@your-institution.edu
```

输出：`pdfs/<doi_safe>.pdf` + `pdf_index.csv`（每条记录附
`status`、`source`、`url`、`pdf_path`、`error`）。

我们的测试中，IPCC 气候 / 环境类引用文献的 OA 覆盖率约 40–60%。

### 第 5 阶段 — `markdown`：PDF → Markdown

按优先级尝试 Microsoft MarkItDown → `pymupdf4llm` → 纯 PyMuPDF 文本
抽取。每个文件实际使用的 converter 被记录下来，便于事后审计转换质量。

```bash
python pipeline_extras.py markdown \
    --pdfs output/stage4_pdfs \
    --out output/stage5_markdown \
    --converter markitdown        # 可选: pymupdf4llm, pymupdf
```

输出：`markdown/<doi_safe>.md` + `markdown_index.csv`。

### 第 6 阶段 — `extract`：基于 schema 的 LLM 抽取

对每个 markdown 文件，让 Claude 输出符合固定 schema 的 JSON：

```json
{
  "research_question": "...",
  "field": "...",
  "methods": ["..."],
  "data_sources": ["..."],
  "geographic_scope": "...",
  "time_period": "...",
  "key_findings": [
    {"finding": "...", "evidence_quote": "...", "is_quantitative": true}
  ],
  "stated_uncertainty": "...",
  "policy_relevance": "...",
  "limitations": ["..."],
  "ipcc_relevance_tags": ["..."]
}
```

每条 finding 必须附 `evidence_quote`（原文逐字引用片段）。这是
**可审计性的锚点**：抽样核对就能判定模型是否在编。

```bash
# 通过 --api-key 传入，或在环境变量里设 ANTHROPIC_API_KEY
export ANTHROPIC_API_KEY=sk-ant-...

python pipeline_extras.py extract \
    --markdown output/stage5_markdown \
    --out output/stage6_extracted \
    --model claude-sonnet-4-6 \
    --max-papers 20               # 先跑 20 篇试试；正式跑去掉这一行
```

输出：`extracted/<doi_safe>.json` + `extracted_index.csv`。已抽取过
的文件会自动跳过。

**成本提示。** 一篇 30 页论文输入约 30–50k tokens，输出 ≤4k。按
Sonnet 4.6 list 价格，10,000 篇语料通常落在四位数美元区间。
**正式跑之前务必先在 `--max-papers 20` 上把 prompt / schema 跑通。**

### 第 7 阶段 — `analyze`：文献计量分析 + HTML 报告

将 WoS 元数据（来自 `Record.csv`）与 LLM 抽取出的维度（来自
`extracted/*.json`）联合分析：

```bash
python pipeline_extras.py analyze \
    --records output/Record.csv \
    --extracted output/stage6_extracted \
    --out output/stage7_analysis
```

输出：

- `analysis_report.html` —— 自包含 HTML 报告，含内嵌图表，涵盖：
    - 传统文献计量：年份分布、Top 期刊、Top 作者。
    - LLM 衍生维度：研究方法分布、数据源分布、地理范围、IPCC 主题标签、
      作者自述局限性。
- `analysis_tables.xlsx` —— 原始计数表（多个 sheet），便于二次分析。
- `figures/` —— 单独的 PNG 图表，方便插入论文 / 演示。

### 一键运行整条流水线

```bash
python pipeline_extras.py all \
    --records output/Record.csv \
    --out output/full_pipeline \
    --email you@your-institution.edu \
    --max-papers 10           # 先 10 篇试跑，再去掉这行
```

---

## 5. 安装

在 macOS / Linux / Windows + Python 3.9–3.12 上测试通过。

```bash
# 1. 克隆
git clone https://github.com/<your-account>/ipcc-refs-toolkit.git
cd ipcc-refs-toolkit

# 2.（推荐）虚拟环境
python -m venv .venv
source .venv/bin/activate          # macOS / Linux
.venv\Scripts\activate             # Windows

# 3. 安装依赖
pip install -r requirements.txt

# 4.（可选）第 2 阶段浏览器降级方案
playwright install chromium

# 5.（可选，仅 Linux）Tkinter
sudo apt install python3-tk        # Ubuntu / Debian
```

可选包（`markitdown`、`pymupdf4llm`）未安装时会优雅降级到 PyMuPDF
纯文本路径，流水线仍可运行，只是 markdown 输出质量稍低。

---

## 6. 快速上手

第一次小规模跑通的推荐流程：

```bash
# 第 1–3 阶段：GUI
python ipcc_refs_gui.py
# - Tab 1: 选一个小的 IPCC 章节 PDF，Max refs per chapter 设 10
# - Tab 2: 粘 WoS API key（或走浏览器路径）
# - Tab 3: 点 Run

# 第 4–7 阶段：CLI
python pipeline_extras.py all \
    --records output/Record.csv \
    --out output/full \
    --email you@example.com \
    --max-papers 5
```

跑完后用浏览器打开 `output/full/stage7_analysis/analysis_report.html`
查看报告。

---

## 7. API 配置

### Web of Science Starter API（第 2 阶段）

- Endpoint：`https://api.clarivate.com/apis/wos-starter/v1/documents`
- 文档：<https://developer.clarivate.com/apis/wos-starter>
- **粘在哪：** GUI Tab 2 → 红色高亮的 *"WOS API key (★ RECOMMENDED ★)"* 字段。
- 很多大学的 WoS 订阅自带 API 配额——先问图书馆，不要急着买个人版。

### Anthropic Claude API（第 6 阶段）

- Endpoint：`https://api.anthropic.com/v1/messages`
- 申请 key：<https://console.anthropic.com>
- **怎么传：** 命令行加 `--api-key sk-ant-...`，或 shell 里
  `export ANTHROPIC_API_KEY=sk-ant-...`。
- 默认模型：`claude-sonnet-4-6`（结构化抽取的成本 / 质量最优解）。
  可用 `--model` 覆盖。
- 优先使用官方 `anthropic` Python SDK；未安装则走直接 HTTPS。
  两条路径都能工作。

### Unpaywall（第 4 阶段）

- 不需要 key，只要邮箱（即所谓 polite pool）。
- 命令行传 `--email you@your-institution.edu` 即可。

---

## 8. 输出文件说明

| 阶段 | 文件 | 说明 |
|----:|-----|------|
| 1 | `references.xlsx` | 每行一条提取出的参考文献；15 列 schema。 |
| 1 | `wos_queries.txt` | WoS Advanced Search 格式的 DOI 批查询（每批 50）。 |
| 1 | `crossref_cache.json` | 磁盘缓存；可安全删除。 |
| 1 | `failed_chapters.csv` *（仅失败时）* | 章节级失败的结构化列表（含 error + traceback）。 |
| 1 | `extraction_errors.log` *（仅失败时）* | 人类可读的章节级 traceback。 |
| 2 | `wos_api_batch_NNN.json` | WoS API 原始响应。 |
| 2 | `wos_api_combined.xlsx` | 扁平化、去重后的合并表。 |
| 2 | `wos_batch_NNN.xlsx` | 浏览器路径：每批一个 Excel。 |
| 3 | `Record.csv` | 匹配上 WoS 的参考文献，WoS 完整 schema（约 70 列）。 |
| 3 | `Unrecord.csv` | 未匹配到的参考文献，8 列精简 schema。 |
| 4 | `pdfs/<doi_safe>.pdf` | 下载下来的 OA PDF。 |
| 4 | `pdf_index.csv` | 每条记录的状态 / 来源 / URL / 错误。 |
| 5 | `markdown/<doi_safe>.md` | 转出来的 Markdown。 |
| 5 | `markdown_index.csv` | 每个文件实际用的 converter + 字符数。 |
| 6 | `extracted/<doi_safe>.json` | LLM 结构化抽取结果。 |
| 6 | `extracted_index.csv` | 每篇论文的抽取状态。 |
| 7 | `analysis_report.html` | 自包含 HTML 报告。 |
| 7 | `analysis_tables.xlsx` | 原始计数表（多个 sheet）。 |
| 7 | `figures/*.png` | 单独的 PNG 图表。 |
| *所有阶段* | `task_report_*.txt` | 每次运行的结构化摘要。 |
| *所有阶段* | `failed_items_*.csv` | 每次运行的逐项失败明细（含 traceback）。 |

---

## 9. 法律与伦理说明

**本工具只使用合法的 OA 渠道获取全文。** Sci-Hub 和绕过 paywall 的
做法**有意不予支持**。

- Unpaywall、Crossref、OpenAlex、arXiv 都是研究界标准、合规的 API。
- Crossref `link` 字段中 `intended-application = text-mining` 的 URL，
  是出版商明确为 TDM（文本与数据挖掘）开放的链接。
- 如果你所在机构与 Elsevier / Wiley / Springer 等签有 TDM 协议，可以
  通过扩展 `acquire_one_pdf()` 添加对应 endpoint——但要先和你所在机
  构的图书馆沟通。

第 2 阶段的 WoS 浏览器降级方案，走的就是你手动用的同一套 WoS 搜索
界面。它对 Clarivate 的 ToS 比较敏感；只要有 API 接入，就尽量走 API。

LLM 抽取阶段：

- 任何基于本流水线产出的发表物，请在方法部分注明所用模型版本。
- 每条 finding 的 `evidence_quote` 是**刻意要求**的、不可省略的——
  没有原文 grounding 的 LLM "总结"不是可复现研究，不应进入文献。
- 在大规模得出结论之前，对抽取结果做随机抽样审计（建议比例 5–10%）。

---

## 10. 已知限制

**没有 WoS API key 时**，第 1 阶段仍可在本地完整运行，产出可用的
references 数据库；第 2 阶段的浏览器降级方案能跑，但 ToS 敏感、
且要求电脑全程开机。

**第 4 阶段的 OA 覆盖率受限于底层文献本身的开放度。** 气候 / 环境
类 IPCC 引用，下载成功率约 40–60%；越老或越偏理论的文献越低。
`pdf_index.csv` 把"缺口"显式记录下来，便于在派生研究中诚实地把它
报告成 limitation。

**MarkItDown 处理学术 PDF 的质量参差不齐。** 双栏、公式、复杂表格、
扫描页都难处理。`markdown_index.csv` 记录了每个文件实际用的
converter，便于事后审计。已知质量差的 PDF，可以先 `ocrmypdf` 预处理。

**LLM 抽取有实际的假阳性率。** schema 要求逐字 evidence_quote 就是
为了让这类错误可检测——但你必须真的去抽样审计（建议至少 5–10%）才能
发表聚合结论。

**成本。** Sonnet 4.6 在 10,000 篇 × 约 40k 输入 token 的规模下，
通常落在四位数美元区间。**正式跑前一定先 `--max-papers 20` 试跑。**

**参考文献切分** 在第 1 阶段是启发式的，假设的是 author-comma-initial
引用格式。若 PDF 用的是带方括号的编号引用或其他非常规格式，需要自定
义切分逻辑。

---

## 11. 开发路线图

1. **WoS Expanded API 支持**：在第 2 阶段补上 Cited References、
   funding、addresses 等完整 schema。
2. **持久化配置文件**（`~/.ipcc_refs_toolkit.yaml`）保存 API key、
   默认路径等。
3. **GUI 增加第 4–7 阶段的 Tab**，做到单窗口端到端。
4. **Scopus / OpenAlex 富化** 作为第 2 阶段 WoS 的替代选项。
5. **OCR 预处理** 集成，处理扫描版 PDF。
6. **跨文档分析**（引文网络、主题聚类、时序趋势检验）在第 7 阶段。
7. **抽样审计工具**，让第 6 阶段的审计从临时操作变成系统流程。
8. **单元 / 集成测试套件** 含小型样例 PDF。

---

## 12. 作者与许可

**作者：** Jiacheng Zheng

**联系 / 主页：** <https://karcen.github.io/zhengjiacheng.github.io/>

为支撑 IPCC 相关文献计量研究而构建。Bug、PR、反馈都欢迎，通过上面的
主页联系。

仓库未附带 license 文件；请按学术研究软件对待。如需在商业项目或大规
模场景下复用，请先联系作者。
