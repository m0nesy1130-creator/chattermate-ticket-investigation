# Agent 评测

评测使用虚构合同和工单，共 18 道题。12 道文档题分别跑关键词检索与关键词加 BGE 的混合检索，6 道流程题各跑一次。全套共运行 30 次。

[查看 2026-11-13 的实跑基线](baseline.md)。
[查看 2026-11-15 的优化与复测记录](optimization-2026-11-15.md)。
[查看失败恢复与独立评测记录](reliability-2026-11-15.md)。

## 运行

在项目根目录使用 [README 的本机环境](../README.md)和 DeepSeek 凭据。首次下载本地向量模型时需要联网。

```powershell
& '.\.venv\Scripts\python.exe' -X utf8 -m evaluation.run --suite documents
& '.\.venv\Scripts\python.exe' -X utf8 -m evaluation.run --suite workflows --case R01
& '.\.venv\Scripts\python.exe' -X utf8 -m evaluation.run
& '.\.venv\Scripts\python.exe' -X utf8 -m evaluation.run --suite holdout
& '.\.venv\Scripts\python.exe' -X utf8 -m evaluation.recovery --mysql
```

文档组只需要 DeepSeek 和本地向量模型。流程组的 R01 需要可写的 MySQL 检查点库，M01、M02、W01 需要只读业务库。W01 同时需要能访问 Python 官方文档。

`--suite holdout` 使用单独的 [8 道文档题](holdout_cases.json)，并通过 H09 验证源文件更新后的记忆失效。`--case H09` 可只运行真实 MySQL 记忆案例。`--suite all` 保持原有 18 题口径。

运行器逐题保存 JSON 到 `runtime/evaluation/`，也可用 `--output` 指定文件。结果包括运行用时、召回行号、引用行号、评分、流程步骤和报告。R01 的虚构任务会写入指定的检查点库，并在子进程中恢复；可在隔离的检查点库中执行。题目未通过时也会继续运行，自动化调用方可以从 JSON 读取 `summary`。

## 配置可复现的业务数据

1. 在本机 MySQL 8.4 中用管理员账号导入 [business.sql](fixtures/business.sql)，创建独立的 `analysis_agent_eval` 库。
2. 创建一个评测只读账号，例如 `agent_eval_reader`，仅授予 `analysis_agent_eval.posted_payments` 和 `analysis_agent_eval.service_tickets` 的 `SELECT` 权限。
3. 按[付款数据源](../docs/mysql-payments.md#接入自己的台账)和[通用 MySQL 数据源](../docs/multi-source.md#自定义-mysql-数据源)配置下列条目。端口、账号与本机配置对应。

| 配置字段 | 付款数据源 | 工单数据源 |
| --- | --- | --- |
| `id` | `eval_payment` | `eval_ticket` |
| `label` | 评测付款 | 评测工单 |
| `database` | `analysis_agent_eval` | `analysis_agent_eval` |
| `table` | `posted_payments` | `service_tickets` |
| `user` | `agent_eval_reader` | `agent_eval_reader` |
| `port` | `3306` | `3306` |
| `key` |  | `ticket_id` |
| `columns` |  | `["ticket_id", "symptom", "status", "minutes"]` |

密码由 Windows 凭据管理器或环境变量提供。使用表中的数据源 ID 时，环境变量名分别为 `ANALYSIS_AGENT_BUSINESS_EVAL_PAYMENT_PASSWORD` 和 `ANALYSIS_AGENT_QUERY_EVAL_TICKET_PASSWORD`。

```powershell
& '.\.venv\Scripts\python.exe' -X utf8 -m evaluation.run --payment-source eval_payment --query-source eval_ticket
```

本机已有同样样例记录的 `payment_demo`、`ticket_demo` 时，可直接用默认参数运行。检查点连接沿用项目根 README 的配置。

## 评分

文档组复用同一模型、题目和文档。标准结论、状态和证据行在 [document_cases.json](document_cases.json) 中维护。评分按题检查以下条件。

- 结论的复核状态匹配预期。直接支持题的结论需匹配预设内容，冲突题需要说明双方数值。
- 直接支持题引用预设原文行，冲突题须在复核说明中指出两处证据。
- 缺证题需明确标为证据不足。
- 另计预设原文行是否完整进入候选集，用来定位检索遗漏。

| 编号 | 流程与预期 |
| --- | --- |
| C01 | 从模糊请求开始追问。用户指定并确认年费范围后，输出 12,000 元及原文引用。 |
| R01 | 缺附件时保存 MySQL 检查点。新 Python 进程补交附件，复用原计划和已复核的年费结果，引用附件完成分析。 |
| R02 | 缺附件时选择结束。Markdown 报告保留年费及原文引用，明确服务范围证据不足。 |
| M01 | 按合同编号和年度查到指定两笔付款，付款合计 5,000 元，余额 7,000 元。 |
| M02 | 台账无对应记录，保留缺证状态，余额步骤等待前置证据。 |
| W01 | 引用指定目录的项目版本、Python 官方原文的异常类型，以及 MySQL 的工单计数。 |

评分采用预设状态、正则和引用校验。逐题 JSON 便于人工核对边界表述。真实模型和公开网页可能随时间变化。
