# RabbitMQ：文档上传、仓库同步与项目索引

## 现在的数据流

```mermaid
flowchart LR
    UI[网页上传/连接/刷新] --> API[FastAPI]
    Hook[Webhook/定时同步] --> API
    API --> DB[(PostgreSQL: 业务记录 + BackgroundJob)]
    DB --> Dispatcher[Worker 内的发送箱扫描器]
    Dispatcher -->|发布确认| MQ[RabbitMQ 持久队列]
    MQ -->|手动 ACK| Worker[Worker: 仓库锁 + 状态判断]
    Worker --> Doc[解析/切片/Embedding/Milvus]
    Worker --> Sync[GitHub Issue/PR/Review/CI 同步]
    Worker --> DB
    Worker -->|不可重试或耗尽次数| DLQ[死信队列]
    UI -->|轮询 job_id| API
```

HTTP 接受请求时只保存原文件和任务，不在请求内解析文档或完整同步 GitHub。两个上传入口均已接入：

- `POST /api/rag/repositories/{repo_id}/documents`
- `POST /api/repos/{repo_id}/knowledge/upload`（保留旧响应的 filename/chunks/indexed 字段，新增 job_id/status）
- `POST /api/repos/{repo_id}/memory-index/scan`（返回 project.index 的 job_id，复用仓库锁与去重）

它们返回 HTTP 202；`indexed=false` 表示尚未完成向量入库。连接仓库、手动同步、Webhook、定时同步使用同一任务服务。连接仍需先验证仓库元信息，验证后仓库记录与初始任务一起提交。

连接仓库和手动刷新现在按 `GitHub 同步 → 代码检出/分析 → 项目文档索引` 串联执行。任务与 building 状态一起提交，检出失败不会被当成索引完成。手动扫描走 `project.index`；目录尚未准备好时先完成检出，目录已存在则只重新扫描，不强制联网。它与同步共享同一个仓库互斥锁，因此提前扫描、重复点击和 API 重启不会再让请求内后台扫描抢在克隆之前失败。重复活动扫描共用 job_id；终态消息重投不会重新扫描，后续手动更新仍可创建新任务。

## 幂等具体怎么做

1. **提交与发布分开。** 业务记录和 BackgroundJob 在同一 PostgreSQL 事务内提交。任务表兼作发送箱；RabbitMQ 不可用时任务保留，恢复后补投。只有收到 Broker 发布确认才记 published_at。
2. **合并重复请求。** 相同仓库、相同同步参数的活动任务共用唯一 active_key。参数不同不会被错误合并。终态释放该键，允许之后再次刷新。Webhook 的 `X-GitHub-Delivery` 另存唯一 request_key，同一事件即使完成后重发也复用原任务。
3. **同仓库互斥。** Worker 用独立连接持有 PostgreSQL 会话级 advisory lock；业务内部 commit 不会释放锁。相同仓库的任务串行执行；不同仓库可并行。进程退出后连接关闭，锁自动释放。SQLite 只是单元测试适配器，正式 Worker 强制 PostgreSQL。
4. **上传防重复。** 同仓库、同 SHA-256 的上传受独立锁保护；已有正常或正在重试的文档会复用源记录和活动任务。并发抢锁失败返回 409，客户端稍后重试。失败终态允许重新上传生成新源，或对原源调用 retry。
5. **重复消息先查状态。** completed/failed/cancelled 不再执行业务。领取任务会持久化尝试次数和 worker_token；成功或失败状态提交后才 ACK/reject。文档源已完成而任务未提交时，重投递会直接校正任务为 completed，包括最后一次尝试中断的情况。
6. **业务重跑不累加。** GitHub 数据按仓库及远端 ID 更新；PR 文件与评论按组替换。文档重跑按 source_id 清理旧切片和向量再写入，清理包括 Milvus 中该源的孤立数据。
7. **删除协调。** 删除与处理争用同一仓库锁，正在处理时返回 409。删除排队文档会将任务取消并删除源文件；之后的旧消息不会重建它。项目知识页和知识库页的删除都协调队列任务。

这是“至少一次投递 + 幂等执行”，不保证函数只调用一次。PostgreSQL 与 Milvus 没有跨库事务；中断后依赖按源清理重跑恢复一致性。最终失败仍需检查错误并重试。

## 状态、重试、死信

状态流转：`queued → running → completed`；临时故障为 `running → retrying → running`；永久故障或耗尽次数为 `failed`；删除排队任务为 `cancelled`。

- 默认最多 3 次业务尝试，普通临时错误约 5 秒、10 秒退避。
- 网络错误、HTTP 408、5xx 可重试；429 和明确的 GitHub 限流 403 参考 Retry-After/重置时间，单次延迟最多 1 小时。
- 不可解析的空文档、文件缺失、权限错误、普通 4xx 直接失败。
- Worker 对 Issue、PR、PR 文件/评论、CI Run/Job/失败日志启用严格错误传播，避免将远端错误当作“同步到空列表”。需要无权访问的 CI 时，可在同步参数中关闭该项或补充权限。
- 新连接和手动刷新要求代码检出、分析及项目索引完成；失败会进入有限重试并将错误同步到索引状态，已提交的 GitHub 数据仍保留。未要求建立索引的旧任务仍保留代码分析 warnings 的兼容行为。
- Worker 退出时未 ACK 消息会重投；发送箱也默认每 30 秒补投未结束任务。长任务可能收到重复消息，仓库锁会阻止并发重跑。
- 持久队列：`devflow.document.ingest`、`devflow.repo.sync`、`devflow.project.index`、`devflow.memory.enrich`。各有 `.dead` 死信队列；非法消息和最终失败消息进入死信。

死信用于排查，直接重放旧 failed 任务不会让它重新执行。文档调用 `POST /api/rag/repositories/{repo_id}/documents/{document_id}/retry` 创建新的尝试任务；同步再次调用 `POST /api/repos/{repo_id}/sync`。活动文档任务重复 retry 会复用原 job_id。死信暂不自动清理，长期部署应按运维策略归档清理。

## 本机启动与查看

在项目根目录执行：

```powershell
powershell -ExecutionPolicy Bypass -File scripts/start_full.ps1
# 只重启本项目 Worker（保留任务、数据库和队列）
powershell -ExecutionPolicy Bypass -File scripts/restart_worker.ps1
# 停止本项目进程和基础设施，保留数据卷与上传文件
powershell -ExecutionPolicy Bypass -File scripts/stop_full.ps1
```

Worker 由本机 Python 虚拟环境运行，RabbitMQ 由 Docker Compose 运行；API 与 Worker 共用 backend 工作目录、数据库配置和上传目录。首次运行前需要安装 backend/requirements.txt，并在 frontend 构建。现有启动脚本检查前端 BUILD_ID，避免启动不存在的构建。

| 地址 | 用途 |
|---|---|
| http://127.0.0.1:3001 | 项目网页 |
| http://127.0.0.1:8000/docs | API 文档 |
| http://127.0.0.1:15673 | RabbitMQ 管理台 |

本机 RabbitMQ 默认用户 `devflow`，密码 `devflow-local`，vhost `devflow`，AMQP 端口 5673。端口仅绑定回环地址。自定义凭据时同时设置 Compose 的 RABBITMQ_USER/RABBITMQ_PASSWORD 和后端 RABBITMQ_URL，URL 中特殊字符须编码；已有 RabbitMQ 数据卷不会因更改默认环境变量而重置账户。

后端配置项（backend/.env，可覆盖默认值）：

```dotenv
RABBITMQ_URL=amqp://devflow:devflow-local@127.0.0.1:5673/devflow
TASK_MAX_ATTEMPTS=3
TASK_RETRY_SECONDS=5
TASK_DISPATCH_INTERVAL_SECONDS=2
TASK_REPUBLISH_SECONDS=30
TASK_PREFETCH_COUNT=2
```

任务查询：

- `GET /api/repos/{repo_id}/jobs/{job_id}`：状态、尝试次数、错误、时间、结果。
- `GET /api/repos/{repo_id}/jobs`：最近 30 个任务。
- `GET /api/repos/{repo_id}/sync-status`：最近同步任务及 last_sync_at。
- 文档列表：处理阶段和关联 job_id/job_status/job_attempts。

仓库页面刷新后会恢复活动同步任务的轮询；短暂网络错误、503 和 429 会继续查询，最长跟踪 10 分钟，超时后可刷新继续查看。文档列表保持解析、切片、Embedding 阶段展示。

日志、进程记录和 Worker 心跳在 `.data/runtime/`。心跳包含实例 ID，兼容 Windows 虚拟环境的 Python 启动器与实际子进程 PID 不同。停止脚本同时校验 PID 和启动时间，避免误停复用 PID 的进程，兼容 PowerShell 5 和 7。

## 可重复验证

```powershell
# 后端测试（从项目根目录）
Push-Location backend
.\.venv\Scripts\python.exe -m pytest app/tests -q -p no:cacheprovider --basetemp=../.data/pytest-mq-manual
Pop-Location

# 真实 Broker / PostgreSQL / 配置的 Embedding / Milvus 集成验证
.\backend\.venv\Scripts\python.exe scripts/verify_rabbitmq.py
# 另加 Worker 强制中断、旧消息重放、RabbitMQ 停机后发送箱恢复
.\backend\.venv\Scripts\python.exe scripts/verify_rabbitmq.py --faults
# 只用本地虚构 Git 仓库，实测提前扫描、并发点击、自动串联和终态重复投递
.\backend\.venv\Scripts\python.exe scripts/verify_project_index_pipeline.py
```

`--faults` 会暂时停止本项目 Worker 与 `devflow-ai-local-rabbitmq`，finally 中恢复服务；请在允许本机任务短暂停顿时运行。测试建立并清理独立的虚构仓库，不删除已有仓库数据。报告保存为 `.data/runtime/rabbitmq-verification.json`。

GitHub 部分使用本机 HTTP fixture，稳定覆盖 Issue/PR/CI、503 和 403；不是对真实 GitHub 账户权限或外网 API 的验收。文档使用当前配置的真实 Embedding 和 Milvus。测试会产生少量 Embedding 请求。

## 代码阅读顺序

`routes/rag.py` / `routes/knowledge.py` / `routes/repos.py` → `services/task_queue.py`（入队、去重、状态机、发送箱）→ `workers/task_worker.py`（AMQP 发布与消费）→ `workers/job_handlers.py` → 原 document_processing / repos_sync 业务服务。

原文件备份在 `.data/rabbitmq-backup-20261018/`。本目录是源码归档，没有 Git 元数据，因此没有自动提交或创建 PR。

## 本次验收结果（2026-10-18）

- 后端完整测试：250 passed，包含 31 个新增队列与恢复测试。
- TypeScript 类型检查、Next.js 生产构建成功；4 个轮询故障场景通过。构建保留项目原有 Hook/Browserslist 提示。
- 实际集成：12 个场景全部通过，涵盖数据库并发去重、仓库互斥、双上传入口、向量数量、图谱、重复消息、临时/永久失败、CI 子请求重试、Worker 中断、删除取消和 Broker 停机恢复。
- 报告：`.data/runtime/rabbitmq-verification.json`；最终状态 passed、测试数据清理 HTTP 200。
- 原文件备份 23 个；用户现有仓库及数据保留。应用和 Worker 保持运行。
