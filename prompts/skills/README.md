# skills 法律文书解析技能

本目录存放法律文书解析的提示词技能（Prompt Skills），供大模型在文书识别流程中加载执行。整体遵循「先判型、再提取」的两段式设计：

1. **类型判定**：`document-type-classification` 先判定文书类型（仅判定，不提取字段）
2. **分类型提取**：根据判定结果，选择且仅选择对应的一个提取技能，按固定 JSON 结构输出字段

编排规则见 [AGENT.md](./AGENT.md)。

## 目录结构

```
skills/
├── AGENT.md                             # 总控提示词：类型判定 → 分发到对应提取技能
├── README.md                            # 本说明文件
├── document-type-classification/        # 文书类型判定（9 类可解析文书 vs 仅归档材料）
├── complaint-arbitration-extract/       # 起诉状 / 仲裁申请书字段提取
├── evidence-notice-extract/             # 举证通知书字段提取
├── judgment-ruling-mediation-extract/   # 判决书 / 裁定书 / 调解书字段提取
├── defense-notice-extract/              # 应诉通知书 / 参加诉讼通知书字段提取
├── summons-hearing-extract/             # 传票 / 开庭通知书字段提取
├── archived-field-extraction/           # 【已归档】旧版统一字段提取，不再参考使用
└── file-categorization/                 # 按人名/案件号整理当前文件夹文件（独立工具技能）
```

## 技能清单

| 技能 | 适用文书 | 提取字段数 | 说明 |
| ---- | ---- | ---- | ---- |
| `document-type-classification` | 全部 | - | 仅判定类型，区分「需完整解析的 9 类文书」与「仅归档存储的材料」 |
| `complaint-arbitration-extract` | 起诉状 / 仲裁申请书副本 | 8 | 原告/申请人、被告/被申请人、涉及主体、业务类型、标准案由、受理法院/仲裁委、标的额、诉讼请求金额 |
| `evidence-notice-extract` | 举证通知书 | 7 | 案号、被告/被申请人、涉及主体、受理法院/仲裁委、标准案由、业务类型、举证期限 |
| `judgment-ruling-mediation-extract` | 判决书 / 裁定书 / 调解书 | 11 | 案号、当事人、涉及主体、受理法院/仲裁委、业务类型、标准案由、标的额、赔偿金、诉讼请求金额、裁判结果等 |
| `defense-notice-extract` | 应诉通知书 / 参加诉讼通知书 | 8 | 案号、当事人、涉及主体、业务类型、标准案由、受理法院/仲裁委、立案日期 |
| `summons-hearing-extract` | 传票 / 开庭通知书 / 改期开庭通知书 | 8 | 案号、被告/被申请人、涉及主体、受理法院/仲裁委、应到时间、标准案由、业务类型、应到地点 |
| `archived-field-extraction` | （旧版 9 类统一提取） | 15 | **仅存档，不再参考使用** |
| `file-categorization` | - | - | 独立工具技能：按案件号/人名归档当前文件夹文件，禁止删除、重命名、改内容 |

> 类型判定结果为「仅归档存储」或「未知类型」时，不执行任何字段提取，按对应技能规则终止。

## 通用硬性约束

所有提取技能共同遵循（详见各 SKILL.md 与 AGENT.md）：

1. 仅从文书原文提取真实存在的信息，禁止编造、推断或补全
2. 除指定字段外，不输出任何推理结果、分析意见或额外内容
3. 输出为固定 JSON 结构；文书不在可解析范围内时立即终止并输出 `{"错误": "该文书不在可解析范围内"}`
4. 禁止修改、移动、删除任何文件

## 文件规范

每个技能一个文件夹，包含：

- `SKILL.md`：技能正文，以 YAML frontmatter 开头（`name` + `description`），正文包含适用范围、硬性约束、字段定义、执行步骤与输出格式
- `.修改记录.md`：该技能的迭代修改日志（隐藏文件）

修改或新增技能时：

1. 保持 frontmatter 的 `name` 与文件夹名一致
2. 提取字段调整须同步更新 `description` 中的字段清单
3. 在对应 `.修改记录.md` 中追加变更记录
4. 涉及类型分发关系变更时，同步更新 `AGENT.md`
