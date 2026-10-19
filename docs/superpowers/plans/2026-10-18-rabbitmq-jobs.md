# RabbitMQ Jobs Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [x]`) syntax for tracking.

**Goal:** 将文档入库和仓库同步改为可恢复、幂等的 RabbitMQ 任务。

**Architecture:** PostgreSQL BackgroundJob 兼作发送箱，RabbitMQ 提供持久投递，独立 Python Worker 执行业务；状态查询保留在 API。资源互斥、数据库终态和有限重试支持至少一次投递。

**Tech Stack:** FastAPI、SQLAlchemy、PostgreSQL、aio-pika、RabbitMQ、Next.js。

**Spec:** docs/superpowers/specs/2026-10-18-rabbitmq-jobs-design.md

## Global Constraints

- 使用当前仓库，保留已有数据；修改前文件备份到 .data/rabbitmq-backup-20261018。
- 消息不携带凭据、文件二进制或数据库会话。
- 默认最多尝试三次；永久失败立即终止，临时失败退避。
- 同一仓库执行串行；不同同步参数不能被错误合并。
- 不将任务已接受显示为同步完成。

## Review Focus

- 业务已提交但 RabbitMQ 确认丢失：重复消息不能重复入库。
- API 已提交但 Broker 不可用：任务不能丢失。
- 同一仓库并发同步及文档重试：不能覆盖运行中结果。
- 文档或仓库删除后的旧消息：不能重建资源。
- 前端刷新或网络中断：能继续查看持久化任务状态。

## Task 1: 持久任务、幂等与发送箱

**Files:** backend/app/db/models.py; backend/app/services/task_queue.py; backend/app/services/job_locks.py; backend/app/tests/test_task_queue.py。

**Interfaces:** enqueue_job(db, kind, repo_id, resource_id, payload); process_job(job_id, session_factory, handlers); publish_pending(publisher, session_factory)。

- [x] 先写上传接口 job_id 契约及实际数据库测试，观察缺失功能导致失败。
- [x] 实现任务模型、唯一活动键、互斥和退避状态机。
- [x] 验证重复任务、重复消费、未来重试、终态、发布失败与恢复。

## Task 2: 业务入口、Worker 与前端

**Files:** routes/rag.py、repos.py、webhooks.py；scheduler.py；document_processing.py；schemas；workers/task_worker.py；frontend/lib/jobs.ts；两个仓库页面。

- [x] 测试两个入口返回可查询任务，处理不在请求内执行。
- [x] 实现文档处理异常传递、活动任务复用及删除协调。
- [x] 增加 aio-pika 持久队列、发布确认、手动 ACK、死信与 dispatcher。
- [x] 更新连接、同步、Webhook、定时入口及前端轮询与恢复。
- [x] 跑相关测试与前端类型检查。

## Task 3: 部署与实际验证

**Files:** requirements.txt、config.py、Docker Compose、启动停止脚本、运行说明、集成验证脚本。

- [x] 安装依赖、启动 RabbitMQ 和 Worker，检查队列与 API 状态。
- [x] 验证真实任务、重复投递、Worker 重启及依赖失败恢复。
- [x] 运行完整后端测试与前端构建，记录证据。
- [x] 完成独立代码审查，修复重要问题并补测试。
