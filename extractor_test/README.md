# 法律文书解析提取系统架构说明

> 整理时间：2026-08-19
> 对象：`/Users/olof.chenx2x.net/s4/langchain.ipynb`；`extractor_test/parse_doc.py`、`extractor_test/batch_test.py` 为同逻辑的独立脚本/批处理版本

---

## 一、整体流程

```
PDF
 └─ MinerU 异步解析服务 (BASE_URL /tasks) ──► result(OCR/MD 全文) = query1
                                            │
                            ┌───────────────▼───────────────┐
                            │ ① 判型  classify_agent        │
                            │    load_skill(file-type-       │
                            │      classification)           │
                            │    输出 文书类型 / 是否需解析    │
                            └───────────────┬───────────────┘
                                            │ 需解析 & 命中类型
                            ┌───────────────▼───────────────┐
                            │ ② 提取 × N 次 vote（默认 20） │
                            │    每个 voter:                 │
                            │      agent(extract_prompt)     │
                            │      → align_to_ref(规整字段)  │
                            │      → number_provenance_fails │
                            │         单值编辑距离 OR 子集和  │
                            │      ──不通过──► 带还原值/候选  │
                            │        提示重生成(≤MAX_REGEN)  │
                            └───────────────┬───────────────┘
                                            │
                            ┌───────────────▼───────────────┐
                            │ ③ 投票 majority_vote          │
                            │    每字段取众数 + 众数出现次数   │
                            └───────────────┬───────────────┘
                                            │
                            ┌───────────────▼───────────────┐
                            │ ④ 输出包装                    │
                            │    先溯源(编辑距离)            │
                            │    再 normalize_value 归一化    │
                            │    金额字段 restore_amount_    │
                            │      precision 还原全精度       │
                            │    置信度 = 众数/N × 100       │
                            └───────────────┬───────────────┘
                                            ▼
              {字段: {value, 编辑距离, 置信度}, 提取说明, 文书类型}
```

**关键数据流约定**
- 源头（source）= `query1` = PDF 解析后的全文文本
- 编辑距离 = 字段值（最终值）与原文最佳匹配的最小 Levenshtein 距离
- 置信度 = N 次 vote 中众数出现次数 / N 的百分数
- 数字字段 = `标的额` / `赔偿金` / `诉讼请求金额`（`NUMBER_FIELDS`）
- 加算字段 = `诉讼请求金额`、`标的额`（输出可能是若干原文金额之和，用子集和验证）
- 重生成只由「数字字段溯源不通过」触发，非数字字段永不重生成

---

## 二、配置与常量

| 常量 | 取值 | 作用 |
|---|---|---|
| `BASE_URL` | `http://192.168.10.250:8000` | MinerU 异步解析服务地址 |
| `PDF_PATH` | 本地 PDF 路径 | 待解析文书 |
| `TYPE_TO_SKILL` | 关键词 → skill 目录 映射 | 文书类型关键词匹配提取 skill |
| `SKILL_FIELDS` | skill → 字段清单 | 各类型可提取字段 |
| `NUMBER_FIELDS` | `["标的额","赔偿金","诉讼请求金额"]` | 数字字段（溯源/归一化/还原） |
| `SKIP_PROVENANCE_FIELDS` | `["应到地点","业务类型"]` | 不做溯源的字段（编辑距离为 None） |
| `SUMMED_FIELDS` | `["诉讼请求金额","标的额"]` | 可能由原文若干金额加算的字段 |
| `CLAIM_TRIGGERS` | 诉讼请求区触发词 | 定位原文金额候选的窗口锚点 |
| `NUM_TOL` | `0` | 单值编辑距离容差 |
| `AMOUNT_TOL` | `0.0051` | 金额舍入容差(元)：仅供还原用，溯源判定不用 |
| `MAX_REGEN` | `2` | 每个 voter 内数字字段溯源不通过的最大重生成次数 |
| `AMOUNT_TOKEN_RE` | 带单位的金额正则 | 排除年份/案号，只认带单位金额 |

---

## 三、函数说明

### 3.1 基础设施（skill 加载 / agent / JSON）

- **`skill_for_doc_type(doc_type) -> str | None`**
  按 `TYPE_TO_SKILL` 用关键词子串匹配，把文书类型名映射到提取 skill 目录名；匹配不到返回 `None`（无需提取）。

- **`load_skill(skill_name) -> str`**（`@tool`，暴露给模型）
  读取指定 skill 目录下 `SKILL.md` 全文作为提取/判定规则。支持目录名精确匹配、模糊包含匹配、中文别名兜底（如 `传票` → `summons-hearing-extract`）；找不到时返回可用 skill 列表。

- **`make_agent(system_prompt=None)`**
  构建 agent：默认挂 `SkillsMiddleware`（根目录 `prompts/skills`）+ `load_skill` 工具，通过 `system_prompt` 传指令，避免额外 system 消息导致服务端模板报错。

- **`_ref_fields(skill_name) -> list[str]`**
  读该 skill 的 `ref.json`，取正常分支的 `properties` 键并去掉 `提取说明`，得到字段清单（用于规整与校验输出）。

- **`extract_json(text) -> dict`**
  从模型输出中提取首个合法 JSON 对象：先剥掉 json 代码块围栏（json 或纯文本），再逐 `{` 位置 `raw_decode`，容忍前后夹杂的分析文本。

- **`align_to_ref(obj, skill_name) -> dict`**
  把模型输出按 `ref.json` 规整：只保留清单字段，缺失补 `null`；模型输出若已包成 `{value: ...}` 则取 `value`；`提取说明` 原样保留。返回 `{字段: 值}`。

- **`majority_vote(results) -> (dict, dict)`**
  对多次生成结果逐字段取众数（按 JSON 序列化比较，支持 list/dict 值，平局保留最先出现）。返回 `(众数结果, 每字段众数出现次数)`。

### 3.2 全字段溯源（编辑距离）

- **`_normalize_number(s) -> str`**
  归一化数字写法：去空白、千分位逗号/顿号、`元/人民币/万元/亿` 等后缀、全角转半角，便于数字比较。

- **`_best_token_dist(value, text) -> int | None`**
  归一化后的数字串与原文中**每个数字 token** 的最小 Levenshtein 距离；原文无 token 返回 `None`。

- **`_min_substring_edit_dist(value, text) -> int`**
  文本值与原文**任意连续子串**的最小编辑距离（子串首尾可自由裁剪）；先做逐字命中快速路径，未命中再跑 DP。

- **`_edit_dist_for(value, text) -> int | None`**
  单个字段值的溯源距离：数字走 `_best_token_dist`（保留小数不截断），列表取各元素距离最大值（最差项），文本走子串编辑距离；null/空值 → `None`（不做溯源）。

- **`field_provenance(extracted, text) -> dict`**
  全字段溯源，返回 `{字段: 编辑距离}`；`SKIP_PROVENANCE_FIELDS` 中的字段固定 `None`。

### 3.3 金额解析与归一化

- **`_parse_amount(s)`**
  金额串 → 数值（元）：`万元 ×10000`、`亿元 ×1亿`、去千分位/单位/全角数字；**带小数返回 float 全精度，整数金额返回 int**；无法解析返回 `None`。

- **`normalize_value(value, field)`**
  字段值归一化（在溯源之后执行）：`NUMBER_FIELDS` 统一转数值(元)，复用 `_parse_amount`；递归处理 dict/list；非数字字段原样返回。

### 3.4 数字字段溯源兜底重生成

- **`_source_amount_values(text, window=100) -> list[int]`**
  从原文诉讼请求区抽取金额候选：各 `CLAIM_TRIGGERS` 触发词后 `window` 字符内匹配带单位金额（`AMOUNT_TOKEN_RE`）并解析为数值。

- **`_scale_to_int(vals, target) -> (list[int], int, int, int)`**
  按候选与目标的最大小数位数求缩放，把元金额转成整数（**用 Decimal 精确缩放，不做四舍五入**）；返回 `(整数列表, 目标整数, scale, tol_int)`，其中 `tol_int` 为舍入容差的缩放值，仅供还原用。

- **`_subset_sum_possible(vals, target, tol, max_terms=12) -> bool`**
  判断是否存在不超过 `max_terms` 项之和与 `target` 相差 ≤ `tol`（正数、降序 + 前缀和剪枝回溯）。溯源判定时以 `tol=0` 做**精确**子集和。

- **`number_provenance_fails(cand, text) -> list[str]`**
  数字字段溯源复合判定，返回未通过字段列表：
  1. 单值编辑距离 ≤ `NUM_TOL` → 通过；
  2. 字段 ∈ `SUMMED_FIELDS` 且值 = 原文金额候选的**精确**子集和 → 通过；
  其余判不通过（触发重生成）。金额被四舍五入导致不通过时，由还原与重生成提示兜底修正。null/空/无法解析的值跳过（视为通过）。

- **`_subset_sum_value(vals, target, tol, max_terms=12) -> int | None`**
  返回与 `target` 相差 ≤ `tol` 的**精确子集和**（缩放整数），用于把被模型四舍五入的加算和还原为全精度；找不到返回 `None`。

- **`restore_amount_precision(value, text, field)`**
  把被模型四舍五入（如 `832552.67`）的金额字段值**还原为原文全精度**（如 `832552.665`）：
  1. 直接命中：与原文某金额在 `AMOUNT_TOL` 内 → 用该原文金额；
  2. 加算命中：`SUMMED_FIELDS` 中存在子集和与目标在容差内 → 用该精确和；
  无对应原文金额时原样返回。

- **`_regen_hint(fails, cand, text) -> str`**
  为重生成拼接溯源提示：逐字段列出「提取值无法由原文金额验证」、`restore_amount_precision` 还原出的**最接近原文金额**（请模型按此输出），以及原文诉讼请求区金额候选（如需加算请按各项之和）。

### 3.5 Prompt 与主流程

- **`CLASSIFY_PROMPT` / `classify_agent`**
  判型 agent：调用 `file-type-classification` skill，按关键词命中判定文书类型 / 是否需解析 / 判定依据。

- **`extract_prompt(fields) -> str`**
  构造提取 agent 指令：先 `load_skill` 读对应 skill，再按「字段清单」严格逐字段提取、填 `提取说明`、只输出 JSON。

- **主流程（voter 循环 + 输出包装）**
  1. 判型；`skill_for_doc_type` 命中且需解析时进入提取；
  2. 每个 voter：`extract_agent` 提取 → `align_to_ref` 规整 → `number_provenance_fails` 判定；不通过则带 `_regen_hint` 提示重生成，最多 `MAX_REGEN` 次；
  3. `majority_vote` 取众数；
  4. `field_provenance` 先对原始输出溯源，再 `normalize_value` 归一化；金额字段经 `restore_amount_precision` 还原全精度，金额字段的 `编辑距离` 按还原后的值重算；
  5. 输出 `{字段: {value, 编辑距离, 置信度}, 提取说明, 文书类型}`。

---

## 四、置信度语义：vote vs logprob（重要）

本系统字段级 `置信度` 使用 **vote（多次采样众数占比）** 计算，`置信度 = 众数出现次数 / N × 100`，语义上度量「**对当前文件的置信度**」——同一文件在不同采样下是否稳定收敛到同一答案。

与 `logprob`（单条采样路径的 token 对数概率）方案对比，二者度量的是不同维度：

| 维度 | logprob | vote（本系统采用） |
|---|---|---|
| 定义 | 单条采样路径上 token 对数概率（整条路径连乘） | N 次采样后众数占比（`batch_test.py` 的 `majority_vote` + `wrap_extracted`） |
| 回答的问题 | 模型对**这条输出路径**有多自信 | **这个文件**在不同采样下是否稳定收敛到同一结果 |
| 度量对象 | 单次生成、单条路径 | 跨多次生成、对当前文件的整体置信 |
| 量纲 | 0–1 | 0–100 百分数 |
| 序列长度敏感性 | 随长度衰减（连乘），跨字段/跨答案不可直接比较 | 归一化，可比较 |
| 是否能发现文件歧义 | 否（单次生成看不到竞争性答案） | 是（多个竞争答案会分裂投票，vote 变低） |
| 成本 | 1 次生成 | N 次生成（默认 20） |

**两者关系**
- 同源正相关：logprob 高往往 vote 也高（同出自一个底层分布），但**不是因果保证**。
- 多次生成的 logprob **不一致**：采样随机性使每次走不同的 token 路径（temperature>0 时）；只有贪心解码（temperature=0）才逐次相同，但那样 vote 也失去意义。
- 区分场景：存在多个近义竞争答案（如 `832552.665` vs `832552.67`）时，每条路径 logprob 都很高但投票分裂 → vote 低；logprob 无法暴露这一点。

**如何读置信度（判读约定）**
- logprob 高 + vote 高：模型自信且采样稳定，大概率可信。
- vote 低：文件该字段有歧义（多个候选/当事人/日期等），需要人工复核。
- 收敛≠正确：N 次全收敛到同一错误答案（如系统性误判）时 vote 仍为 100%，它度量「稳定/一致」而非「对」。

---

## 五、金额字段防舍入约定（重要）

- 模型输出金额必须**保留原文全部小数位**：skill 侧硬性约束禁止四舍五入/截取小数（见 `prompts/skills/*/SKILL.md` 与各 `.修改记录.md`）。
- 溯源判定**不设金额容差**：`832552.67`（≠ 原文 `832552.665`）会判不通过并触发重生成。
- 重生成提示携带 `restore_amount_precision` 还原出的最接近原文金额，引导模型按全精度输出。
- 最终输出**仍由 `restore_amount_precision` 兜底还原**，保证 value 小数位与原文一致。
