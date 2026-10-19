# 记忆工作台与上下文可靠性改造

用户明确要求更换前端风格和排版、完成对话记忆增删改/时间衰减，并选择截图中的五项缺口一起补齐。沿用已授权的本机开发与部署，不修改密钥、不删除现有聊天。

## 结果与边界

1. 统一浅色靛蓝工作台，侧边主导航、清晰页面层级、响应式布局；新增完整记忆管理页，主会话页可进入。
2. 会话记忆条目可新增、编辑、软删除、恢复、查看版本/回滚、固定、设置有效期和衰减半衰期；并发编辑乐观锁返回 409。
3. 新 MemoryEntry/MemoryRevision/MemoryConflict 表保存事实版本、有效期和冲突。ConversationMemory 是兼容汇总，模型上下文优先读取有效条目，不全量加载旧记忆。
4. 衰减：score = importance * confidence * 2^(-age_days / half_life_days)，age 从明确确认/创建计算，读取不刷新寿命。固定条目不衰减，但显式有效期仍生效。到期/删除/冲突条目不自动召回；TTL 不删除原始聊天。旧记忆按会话懒迁移，删除墓碑防止模型重新提取后复活。
5. 同一明确 fact key 出现不同内容时留冲突记录，待选择保留/替换；不把字符串相似度当事实验证。手动更新属于用户确认并形成新版本。
6. 会话原文与 memory.enrich 发送箱任务同事务提交。RabbitMQ Worker 异步抽取记忆，领取失败可重试，session 已封存则幂等跳过。提取输入有界，并保持旧测试/调用可使用 inline seal。按会话时序防止旧任务覆盖新摘要。
7. 历史消息检索下推数据库并分页；PostgreSQL 使用 simple 全文索引及中文可用的 trigram/ILIKE 回退，SQLite 保持行为测试适配。仓库和会话范围不可越界，兼容旧保留副本过滤。
8. 从持久 AgentWorkflowRun/AgentTaskRun/ActionDraft 构造统一运行状态投影，包含待执行计划、任务状态、待确认操作。每轮上下文和压缩恢复均重新加载当前状态，避免使用压缩前状态当当前状态。
9. 候选知识自动质量校验记录结果、来源、冲突/敏感信息检查。只有有可核对来源且无冲突的条目可按配置自动晋升；没有来源或不能验证的候选继续人工确认。模型给自己打高分不构成验证。

## Backend / Frontend 契约

基础前缀 `/api/repos/{repo_id}/memory`，所有 ID 为 UUID。必须校验 conversation 属于 repo。

- GET `/entries?conversation_id=&q=&status=active|all|deleted|expired|conflict&kind=&limit=50&offset=0` → `{items,total,stats}`。items: `{id,repo_id,conversation_id,kind,key,title,content,status,version,importance,confidence,pinned,half_life_days,expires_at,created_at,updated_at,last_confirmed_at,score,source,metadata}`。stats 至少含 active/expired/deleted/conflicts/total。
- POST `/entries`：`{conversation_id,kind,key?,title?,content,importance?,confidence?,pinned?,half_life_days?,ttl_days?}` → item。kind 为 fact/decision/task/preference/repo_context/summary，importance/confidence 为 0..1，half_life_days > 0，ttl_days:null 为长期有效。
- PATCH `/entries/{id}`：字段补丁与必填 expected_version → item。
- DELETE `/entries/{id}?expected_version=N`：软删除 → item。
- POST `/entries/{id}/restore`：`{expected_version}` → item。
- GET `/entries/{id}/revisions` → `[{version,operation,snapshot,created_at}]`。
- POST `/entries/{id}/revert`：`{expected_version,target_version}` → item。
- GET `/conflicts?conversation_id=` → `[{id,entry_id,current_content,proposed_content,status,created_at,...}]`。
- POST `/conflicts/{id}/resolve`：`{resolution:'keep'|'replace',expected_version}` → item。
- 复用现有候选 GET `/candidates`、POST `/candidates/{id}/approve|reject`；新增 POST `/candidates/{id}/assess` → `{verdict,score,checks,evidence,can_auto_approve}`。质量结果也置于候选 metadata.quality。
- GET `/messages?conversation_id=&q=&limit=20&offset=0` → `{items:[{id,role,content,created_at}],total,search_mode}`。
- GET `/runtime?conversation_id=` → `{plan:{goal,status,tasks}|null,agents:[],pending_actions:[],enrichment:{queued,running,failed},updated_at}`。

## 代码接口

`services/memory_lifecycle.py`：`sync_memory_entries(db,repo_id,conversation_id,memory_update,session_id=None)`；`managed_memory_context(db,repo_id,conversation_id,query='',max_tokens=700)` → `{text,items,stats,managed}`。lazy 迁移需保留已存在墓碑与版本。entry 元数据和 candidate 元数据可以链接 memory_entry_id / memory_entry_version，使 RAG 侧过滤失效版本。

新路由 `api/routes/memory_manage.py` 由主协调者注册。新运行状态/历史路由 `api/routes/memory_runtime.py` 由主协调者实现。新表放现有 models.py，采用 create_all 增量创建，不重建旧表。必要索引单独幂等 bootstrap。

## 验证

后端：CRUD、版本冲突、回滚、删除墓碑、跨仓库隔离、TTL、半衰期、固定但仍可过期、冲突解决、质量校验防伪、异步发送箱、重复消费、失败恢复、数据库分页检索、压缩后加载当前计划。前端：类型检查、生产构建、真实浏览器桌面与窄屏检查，真实增改删恢复操作。最终跑完整测试和独立只读审查。

## 本地工作方式

无 Git 元数据，原文件另备份到 `.data/ui-memory-backup-20261018/`，保留此前 RabbitMQ 实现。将独立 UI 和记忆生命周期实现分配给不同执行者；主协调者负责 Chat/Worker/检索集成，禁止交叉编辑同一文件。按子项目交付并整体验证。
