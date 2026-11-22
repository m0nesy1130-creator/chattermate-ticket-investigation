# ChatterMate 分析工作台

本目录基于开源 ChatterMate 社区版源码修改，上游基线为 `ed6c86d`。保留上游 [Apache 2.0 许可证](LICENSE)和 [NOTICE](NOTICE)，工作台集成使用本仓库根目录的分析引擎。

打开 `http://localhost:3000/workbench` 登录。工作台使用 ChatterMate 的工单列表、权限和内部活动。可从工作台新建工单，也可在收件箱打开对话关联的工单，工单说明会填入分析需求。

工作台新建工单时默认跳过相似工单检查，避免等待首次下载去重模型。可在创建表单上勾选「创建时检查相似工单」。

- **通用分析**支持自由输入需求及可选的 TXT 或 Markdown 文件。先在工单页回答追问和确认范围，再查看执行步骤、引用和待补证据。失败后可继续原调查。完成后可由有审批权限的经办人写入内部工单活动，证据不足或冲突的步骤会保留其状态。
- **合同付款核对**沿用原付款模板，从合同和只读付款台账核对未付余额。

勾选「允许检索公开网页」后，分析引擎可按需检索公开资料。管理员可在本机 `backend/.env` 配置以下来源，再重启本机分析桥接服务。Windows 启动脚本会转发这两项设置。经办人启用该选项后只能使用管理员配置的目录和只读表。

```dotenv
ANALYSIS_AGENT_BRIDGE_DIRECTORY=管理员批准的本地资料目录
ANALYSIS_AGENT_BRIDGE_QUERY_SOURCE=ticket_demo
```

入口在 ChatterMate 工单详情的「合同付款调查」面板。支持上传 UTF-8 TXT 或 Markdown、查看合同及台账引用、补证、继续失败步骤，以及由有审批权限的经办人确认写入工单。未付余额为正且工单处于新建或重新打开时，确认会附加一条内部活动并把工单标为处理中。到账后可用「发起新一轮核对」核查最新台账，再在 ChatterMate 办结。

## 配置本地服务

在这台 Windows 电脑上，首次配置和迁移完成后，双击 `start-local.cmd` 即可启动 Docker、演示台账、分析服务和 ChatterMate，并打开工单列表。PowerShell 入口为 `pwsh -NoProfile -File ./start-local.ps1`。脚本使用本机既有的 Compose 配置和本地镜像。

1. 启动前，按 ChatterMate 自带的 [安装说明](README.md#installation-methods) 从各自的 `.env.example` 配置 `backend/.env` 和 `frontend/.env`。在 ChatterMate 根目录的 `.env` 中设置本地 `CHATTERMATE_POSTGRES_PASSWORD`，并让 `backend/.env` 的 `DATABASE_URL` 使用同一密码。将 `REDIS_URL` 设为 `redis://redis:6379/0`。
2. 为 ChatterMate 的一个组织指定独立的付款台账。将下面的变量加到 ChatterMate 的 `backend/.env`，并替换示例值。组织 UUID 可在登录后的用户信息接口 `GET /api/v1/users/me` 中查看。

   ```dotenv
   ANALYSIS_AGENT_BRIDGE_URL=http://host.docker.internal:8015
   ANALYSIS_AGENT_BRIDGE_ORG_ID=该组织的UUID
   ANALYSIS_AGENT_BRIDGE_TOKEN=两端相同的随机令牌
   ```

3. 在 `analysis-agent` 的 PowerShell 中配置同一个令牌和既有的只读台账 ID。接口只在两个服务之间使用。若两端都直接在本机运行，使用 `127.0.0.1` 监听；ChatterMate 在 Docker Desktop 内时，将桥接服务绑定到容器可访问的主机地址，并限制该端口的访问范围。

   ```powershell
   $env:ANALYSIS_AGENT_BRIDGE_TOKEN = [System.Net.NetworkCredential]::new('', (Read-Host '桥接令牌' -AsSecureString)).Password
   $env:ANALYSIS_AGENT_BRIDGE_SOURCE = 'payment_demo'
   & '.\.venv\Scripts\python.exe' -m uvicorn app.contract_bridge:app --host 127.0.0.1 --port 8015
   ```

   `payment_demo` 是你之前配置的演示付款源。其他环境按 `analysis-agent/docs/mysql-payments.md` 设置业务连接；分析服务同时使用已有 DeepSeek 凭据和 MySQL 检查点库。

4. 在 ChatterMate 项目更新 PostgreSQL 再启动页面。队列在既有 `ticket_investigator` Worker 内运行。开发 Compose 和生产 Compose 都向 API 与 Worker 读取 `backend/.env`。

   ```powershell
   $compose = @('-f', 'docker-compose.yml', '-f', 'docker-compose.override.yml', '-f', 'compose.images-local.yml')
   docker compose @compose up -d --wait db redis
   docker compose @compose run --rm --no-deps backend alembic upgrade head
   docker compose @compose up -d --build
   ```

   本地开发页是 `http://localhost:3000`。登录已配置的组织并打开一张工单，使用「合同付款调查」面板。

## 恢复和回写

- ChatterMate 先提交 PostgreSQL 调查记录，再由 Worker 调用分析服务。该记录的 UUID 同时是分析线程 ID。Worker 崩溃后可在租约到期时被重新领取。每次领取先按原 ID 查状态，已完成的结果直接取回。
- 分析故障显示在当前工单。重试继续原检查点，补证走原任务的回执校验。没有付款记录时可用已有证据结束当前轮次，再发起新一轮核对。
- 确认回写时，在同一个 PostgreSQL 事务里增加内部活动、更新适用的工单状态并标记调查已回写。重复确认读取现有回写结果，不会产生第二条活动。
- 文件和调查结果随工单保存在本地业务库与分析检查点，以便重启后继续。业务台账使用已有只读账号。
