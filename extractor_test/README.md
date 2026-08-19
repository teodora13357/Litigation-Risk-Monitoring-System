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
                            │      → provenance_fails(布尔)  │
                            │         数字:数值匹配/子集和   │
                            │         文本:归一化后子串包含   │
                            │      ──不通过──► 日志(命中+    │
                            │        原文片段) + 带还原值/提示 │
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
                            │    normalize_value 归一化      │
                            │    金额字段 restore_amount_    │
                            │      precision 还原全精度       │
                            │    置信度 = 众数/N × 100       │
                            └───────────────┬───────────────┘
                                            ▼
              {字段: {value, 置信度}, 提取说明, 文书类型}
```

**关键数据流约定**
- 源头（source）= `query1` = PDF 解析后的全文文本
- 置信度 = N 次 vote 中众数出现次数 / N 的百分数
- 数字字段 = `标的额` / `赔偿金` / `诉讼请求金额`（`NUMBER_FIELDS`）
- 加算字段 = `诉讼请求金额`、`标的额`、`赔偿金`（输出可能是若干原文金额之和，用子集和验证）
- 重生成由「字段溯源不通过」触发（除 `SKIP_PROVENANCE_FIELDS` 与 `提取说明`）：数字字段走数值精确匹配/子集和，文本字段走归一化（NFKC+去空白+去标点，保留小数点）后子串包含判定

---

## 二、配置与常量

| 常量 | 取值 | 作用 |
|---|---|---|
| `BASE_URL` | `http://192.168.10.250:8000` | MinerU 异步解析服务地址 |
| `PDF_PATH` | 本地 PDF 路径 | 待解析文书 |
| `TYPE_TO_SKILL` | 关键词 → skill 目录 映射 | 文书类型关键词匹配提取 skill |
| `SKILL_FIELDS` | skill → 字段清单 | 各类型可提取字段 |
| `NUMBER_FIELDS` | `["标的额","赔偿金","诉讼请求金额"]` | 数字字段（溯源/归一化/还原） |
| `SKIP_PROVENANCE_FIELDS` | `["应到地点","业务类型","标准案由"]` | 不做溯源的字段（视为通过） |
| `SUMMED_FIELDS` | `["诉讼请求金额","标的额","赔偿金"]` | 可能由原文若干金额加算的字段 |
| `CLAIM_TRIGGERS` | 诉讼请求区/标的额/赔偿金/裁判区触发词 | 定位原文金额候选的窗口锚点（与各 skill 字段触发词对齐：`诉讼请求`…、`标的额/标的金额/涉案金额/争议金额`、`赔偿金/赔偿款/损害赔偿/赔偿金额`、`裁判`） |
| `DATE_FIELDS` | `["应到时间","立案日期","举证期限"]` | 日期类字段：溯源额外支持年月日数字分组匹配 |
| `AMOUNT_TOL` | `0.0051` | 金额舍入容差(元)：仅供还原用，溯源判定不用 |
| `MAX_REGEN` | `2` | 每个 voter 内字段溯源不通过的最大重生成次数 |
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
  读该 skill 的 `ref.json`，取正常分支的 `properties` 键并去掉 `提取说明`，得到字段清单（用于规整与校验输出）。**按 skill 名缓存**（`@lru_cache`）：每个进程只读一次磁盘；修改 `ref.json` 后需 `_ref_fields.cache_clear()` 或重启进程。

- **`extract_json(text) -> dict`**
  从模型输出中提取首个合法 JSON 对象：先剥掉 json 代码块围栏（json 或纯文本），再逐 `{` 位置 `raw_decode`，容忍前后夹杂的分析文本。

- **`align_to_ref(obj, skill_name) -> dict`**
  把模型输出按 `ref.json` 规整：只保留清单字段，缺失补 `null`；模型输出若已包成 `{value: ...}` 则取 `value`；`提取说明` 原样保留。返回 `{字段: 值}`。

- **`majority_vote(results) -> (dict, dict)`**
  对多次生成结果逐字段取众数（按 JSON 序列化比较，支持 list/dict 值，平局保留最先出现）。返回 `(众数结果, 每字段众数出现次数)`。

### 3.2 全字段溯源（布尔判定）

- **`_text_number_tokens(text) -> tuple`**（`@lru_cache`）
  原文中的数字 token 数值列表（含 `万元/亿元` 等单位换算），按原文缓存供 `_amount_token_match` 复用，避免重复全文扫描与逐 token 解析。全文只扫描一次。

- **`_amount_token_match(target, text) -> bool`**
  目标金额是否与原文某个数字 token **数值相等**（含 `万元/亿元` 换算，如 `80000` 可匹配 `8万元`）。

- **`_text_provenance_ok(value, text, field) -> bool`**
  文本字段溯源布尔判定：列表须**全部**命中；归一化后是原文子串 → `True`；日期类字段额外支持年月日数字分组匹配；null/空/无法归一化 → `True`（跳过）。

### 3.3 金额解析与归一化

- **`_parse_amount(s)`**
  金额串 → 数值（元）：`万元 ×10000`、`亿元 ×1亿`、去千分位/单位/全角数字；**带小数返回 float 全精度，整数金额返回 int**；无法解析返回 `None`。

- **`normalize_value(value, field)`**
  字段值归一化：`NUMBER_FIELDS` 统一转数值(元)，复用 `_parse_amount`；递归处理 dict/list；非数字字段原样返回。

- **数字字段溯源先归一化再比对**
  数字字段（`NUMBER_FIELDS`）溯源判定前，先把提取值与原文金额候选各自经 `_parse_amount` 归一化为数值(元)，再做**严格数值相等**比较（含 `万元/亿元` 换算：`80000` 可匹配 `8万元`，反之 `8` 不能匹配 `8万元`）。无字符串归一化兜底路径。

### 3.4 数字字段溯源兜底重生成

- **`_source_amount_values(text, window=100) -> tuple`**（`@lru_cache`）
  从原文抽取金额候选（**含来源片段**）：各 `CLAIM_TRIGGERS` 触发词（诉讼请求区 / 标的额 / 赔偿金 / 裁判区，与各 skill 字段触发词对齐）后 `window` 字符内匹配带单位金额（`AMOUNT_TOKEN_RE`）并解析为数值。返回 `((数值, 原文片段), ...)`，按数值去重；片段供重生成提示让模型**按多段原文复核**。**按原文缓存**（`provenance_fails` / `_regen_hint` / `restore_amount_precision` 反复调用，全文只扫描一次）；返回 `tuple`（不可变，防缓存被改）。

- **`_scale_to_int(vals, target) -> (list[int], int, int, int)`**
  按候选与目标的最大小数位数求缩放，把元金额转成整数（**用 Decimal 精确缩放，不做四舍五入**）；返回 `(整数列表, 目标整数, scale, tol_int)`，其中 `tol_int` 为舍入容差的缩放值，仅供还原用。

- **`_subset_sum_possible(vals, target, tol, max_terms=12) -> bool`**
  判断是否存在不超过 `max_terms` 项之和与 `target` 相差 ≤ `tol`（正数、降序 + 前缀和剪枝回溯）。溯源判定时以 `tol=0` 做**精确**子集和。

- **`provenance_fails(cand, text) -> list[str]`**
  全字段溯源复合判定（除 `SKIP_PROVENANCE_FIELDS` 与 `提取说明`），返回未通过字段列表：
  - **数字字段**：数值与原文某数字 token **精确相等**（含 `万元/亿元` 换算）→ 通过；字段 ∈ `SUMMED_FIELDS` 且值 = 原文金额候选的**精确**子集和 → 通过；其余判不通过（触发重生成）。
  - **文本字段**：`NFKC`（全角转半角、CJK 兼容字/部首统一到汉字）+ 部首补充块映射 + **去空白 + 去除标点（保留小数点 `.`）**后，为原文子串 → 通过；日期类字段（`应到时间`/`立案日期`/`举证期限`）支持年月日数字分组匹配。
  - null/空/无法解析的值跳过（视为通过）。

- **`_subset_sum_value(vals, target, tol, max_terms=12) -> int | None`**
  返回与 `target` 相差 ≤ `tol` 的**精确子集和**（缩放整数），用于把被模型四舍五入的加算和还原为全精度；找不到返回 `None`。

- **`restore_amount_precision(value, text, field)`**
  把被模型四舍五入（如 `832552.67`）的金额字段值**还原为原文全精度**（如 `832552.665`）：
  1. 直接命中：与原文某金额在 `AMOUNT_TOL` 内 → 用该原文金额；
  2. 加算命中：`SUMMED_FIELDS` 中存在子集和与目标在容差内 → 用该精确和；
  无对应原文金额时原样返回。

- **`_best_source_snippet(value, text, field, win=16) -> (bool, str)`**
  溯源调试信息：返回字段值在原文中的 `(是否命中, 原文片段)`。优先定位原文中的原始值/归一化值并截取 ±win 字符上下文；找不到时返回 `(False, 原文片段)`。用于 voter 溯源失败日志。

- **`_regen_hint(fails, cand, text) -> str`**
  为重生成拼接溯源提示：**数字字段**逐字段列出「提取值无法由原文金额验证」、`restore_amount_precision` 还原出的**最接近原文金额**（请模型按此输出）、以及 `_source_amount_values` 返回的**多段原文金额候选及上下文**（`[金额]「原文片段」`，请模型逐段复核后提取；如需加算请按各项之和）；**文本字段**提示「提取值未能在原文中找到对应内容，请严格按原文提取：只输出原文中明确出现的值，不得改写、推断或补充」。

### 3.5 Prompt 与主流程

- **`CLASSIFY_PROMPT` / `classify_agent`**
  判型 agent：调用 `file-type-classification` skill，按关键词命中判定文书类型 / 是否需解析 / 判定依据。

- **`extract_prompt(fields) -> str`**
  构造提取 agent 指令：先 `load_skill` 读对应 skill，再按「字段清单」严格逐字段提取、填 `提取说明`、只输出 JSON。

- **主流程（voter 循环 + 输出包装）**
  1. 判型；`skill_for_doc_type` 命中且需解析时进入提取；
  2. 每个 voter：`extract_agent` 提取 → `align_to_ref` 规整 → `provenance_fails` 判定（全字段布尔）；不通过则打印溯源日志（字段值 + 是否命中 + 原文片段，见 `_best_source_snippet`）并带 `_regen_hint` 提示重生成，最多 `MAX_REGEN` 次；
  3. `majority_vote` 取众数；
  4. `normalize_value` 归一化；金额字段经 `restore_amount_precision` 还原全精度；
  5. 输出 `{字段: {value, 置信度}, 提取说明, 文书类型}`。

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
- 数字字段溯源**先归一化再比对**：提取值与原文金额候选各自经 `_parse_amount` 归一化为数值(元)后做严格相等比较，仅数值相等通过（含 `万元/亿元` 换算）。
- 重生成提示携带 `restore_amount_precision` 还原出的最接近原文金额，引导模型按全精度输出。
- 最终输出**仍由 `restore_amount_precision` 兜底还原**，保证 value 小数位与原文一致。

---

## 六、调用整合：全文级计算缓存（性能）

一份文书的处理含 **20 个 voter × 最多 3 次提取尝试**，多处以**整篇文书全文**为输入的纯计算被反复调用。已用 `functools.lru_cache` 整合，全文级计算每份文书每种只算 1 次：

| 函数 | 缓存键 | 无缓存调用次数/文书 | 缓存后 |
|---|---|---|---|
| `_provenance_norm` | 输入字符串 | 60~240 次全文 NFKC+去标点 | 1 次（命中率 >95%） |
| `_source_amount_values` | 原文 | 60~220 次全文正则扫描 | 1 次（含多段原文片段） |
| `_text_number_tokens` | 原文 | `_amount_token_match` 每次重复全文扫描+逐 token 解析 | 1 次 |
| `_digit_groups` | 输入字符串 | 日期溯源每次全文 `\d+` 扫描 | 1 次 |
| `_ref_fields` | skill 名 | 60 次读 `ref.json`+JSON 解析 | 每 skill 1 次 |

注意事项：

- **返回类型**：`_source_amount_values` / `_digit_groups` 改为返回 `tuple`（不可变，防止调用方改动污染缓存）。
- **热更新**：`_ref_fields` 按 skill 名缓存，**修改 `ref.json` 后需 `_ref_fields.cache_clear()` 或重启进程**；`load_skill`（读 `SKILL.md`）**不缓存**，改 prompt 即时生效。
- 缓存随进程生命周期常驻，`batch_test.py` / `parse_doc.py` 每次运行即新进程，天然干净；`langchain.ipynb` 同一内核反复运行时，改 `ref.json` 后请手动 `_ref_fields.cache_clear()`。
