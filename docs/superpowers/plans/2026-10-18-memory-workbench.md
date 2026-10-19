# Memory Workbench Implementation Plan

> **For agentic workers:** Use executing-plans for integration and dispatching-parallel-agents for independent files. Steps use checkbox syntax. Requirements: user authorized the complete screenshot scope.

**Goal:** 全量完成工作台改版、可管理的会话记忆和截图中的可靠性缺口。
**Architecture:** 新记忆条目与版本/冲突表作为召回事实源；RabbitMQ 封存整理，数据库全文检索与当前执行状态在上下文装配时合流。前端通过明确 API 契约独立实现。
**Tech Stack:** FastAPI, SQLAlchemy, PostgreSQL, RabbitMQ, Next.js, React, Tailwind。
**Spec:** docs/superpowers/specs/2026-10-18-memory-workbench-design.md

## Global Constraints

- 保留既有聊天、仓库、MQ 任务和密钥；旧文件先备份。没有 Git，不运行 Git 工作树/提交辅助脚本。
- 新代码分文件，避免继续堆积 main page / chat_memory 的业务函数。
- 过期/删除/冲突记忆不自动召回；不得通过读取重置衰减；不把模型自评分当事实来源。
- 不用宽泛清理删除仓库；真实验证只操作本轮精确 fixture ID。

## Review Focus

- 同时编辑与异步记忆提取不能覆盖用户修正。
- 软删除后再次模型抽取不能复活。
- 旧会话任务重试不能覆盖更新的会话摘要。
- 中文检索、空查询、跨仓库 message cursor 不越界。
- 自动知识晋升要防伪引用、无证据、敏感内容和重复批准。

## Task 1: Memory lifecycle and quality gates

Read .superpowers/sdd/2026-10-18-memory-workbench/task-1-brief.md for exact files, API and required behavior.
- [x] Write and observe failing behavior tests.
- [x] Implement new models/service/schema/routes and safe candidate quality assessment.
- [x] Run targeted tests and report integration requirements.
- [x] Independent task review completed; parent resolved remaining alias issue with regression coverage (final re-review unavailable).

## Task 2: Frontend workspace and memory management

Read .superpowers/sdd/2026-10-18-memory-workbench/task-2-brief.md.
- [x] Redesign global navigation, typography, spacing, cards and main workspace layout.
- [x] Build functional memory manager for entries, conflicts, revisions, history, candidates and runtime.
- [x] Type-check; parent performs final production build/browser verification.
- [x] Integration issues identified by independent review; fixes verified by parent (final re-review unavailable).

## Task 3: Chat reliability integration

**Files:** chat_memory.py, chat.py, task_queue.py, workers/job_handlers.py; new session_enrichment.py, message_search.py, runtime_state.py, routes/memory_runtime.py; main.py/config.py.
**Consumes:** Task 1 sync_memory_entries and managed_memory_context signatures; Task 2 consumes spec endpoints.
- [x] Add red tests for queued sealer, duplicate/retry, scoped/paginated message search and current runtime projection.
- [x] Persist raw seal+memory.enrich job atomically; worker enriches with bounded inputs and ordering protection.
- [x] Integrate lifecycle into sealed snapshot/context; filter stale memory-derived RAG knowledge.
- [x] Add PostgreSQL FTS indexes and SQL search API; project live workflow/plan/draft state into every context and compression rehydration.
- [x] Full backend tests; frontend build; real API/MQ/browser verification; parent final audit after independent reviewer became unavailable.

## Task 4: Deploy and handoff

- [x] Restart only project processes as needed, preserve infrastructure and user data.
- [x] Validate new tables/indexes and real memory CRUD, decay, version/conflict and queue behavior.
- [x] Save reproducible report and user documentation, keep application running.
