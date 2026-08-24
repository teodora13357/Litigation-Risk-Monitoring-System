# 重构与准确率提升总结

项目：`/Users/olof.chenx2x.net/s4/Litigation-Risk-Monitoring-System_reconstructing`
分支：`feature/codex`
最终提交：`19632ea`
最终准确率：**91.60%（240/262）**（目标 ≥ 90%）

## 一、用 langgraph 重构提取 pipeline

将 `extractor_test/batch_test.py` 中“判型 → 串行 voter 循环 → 投票收尾”的流程，重构为 langgraph `StateGraph` 状态机：

- `classify`：判型节点
- `parse_check`：解析校验节点（映射提取 skill）
- `extract`：单 voter 提取节点（含溯源失败重生成循环，最多 `MAX_REGEN` 次）
- `collect`：多数投票并包装 `value/置信度`
- 条件边负责“判型失败/无需解析 → END”、“未完成 → 继续 extract 循环”、“完成 → collect”

同时新增：

- `get_agent()` 按 `system_prompt` 缓存 agent，避免重复构建 `create_agent`
- 提取响应非 JSON 时按空对象兜底，避免单篇文书整篇失败

## 二、溯源归一化修复

- `scripts/provenance_utils.py` 的 `_PUNCT_RE` 补齐 Unicode 连字符/破折号变体（`‐ ‑ ‒ – — ― −`），修复 GT 中 `民事诉讼‑侵权纠纷`（U+2011）与结果 `民事诉讼-侵权纠纷`（U+002D）被误判为不一致的问题。

## 三、分类规则修复

- `prompts/skills/file-type-classification/SKILL.md`：9 类可解析文书新增“参加诉讼通知”关键词，修复“参加诉讼通知书”被误判为无需解析。
- `batch_test.py` 的 `CLASSIFY_PROMPT` 同步补充“应诉通知”“参加诉讼通知”示例。

## 四、判决/裁定/调解 skill 修复

- 适用范围扩展为“判决书/裁定书/调解书/仲裁裁决书”，修复 952 号仲裁裁决书整篇被拒解析。
- 案号新增仲裁案号兜底正则：`[\u4e00-\u9fa5]{2,}仲案字[（(]\d{4}[）)]第\d+号`。
- 诉讼请求金额明确“撤回/变更不影响求和范围”。
- 赔偿金新增“裁判主文兜底”。

## 五、传票 skill 修复

- 案由/业务类型映射新增“侵权责任纠纷”，修复“案由=侵权责任纠纷”时业务类型误判为无法推断。

## 六、评测与迭代

- 多次运行 `extractor_test/batch_test.py testset` 与 `extractor_test/batch_eval.py testset`。
- 每次生成 `testset/eval_report.json` 后分析未通过字段，按“OCR 错误 / GT 疑义 / 提取缺口”归类并逐项修复。
- 排查总结已写入 `extractor_test/ai_review/排查问题总结.md`。

## 七、剩余问题（不阻塞 90% 目标）

剩余 22 个字段未通过，主要集中在：

- OCR 层错误：法院名/主体名错字、地址截断、PDF 文本层乱码导致金额误读。
- GT 本身疑义：银行卡号错字、跨文档受理法院、案由与原文不一致、第三人并入被告等。
- 提取缺口：赔偿金兜底执行不稳定、多项诉请求和易错、个别列表字段漏项。
