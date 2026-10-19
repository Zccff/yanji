# DevFlow 演示项目知识：当前有效规则

知识标识：DEVFLOW_LAB_POLICY_V1。以下资料全部是虚构的测试数据。

## 登录错误

过期的演示登录凭证应返回 401，不能返回 500。格式错误凭证同样返回 401。403 表示用户已经通过身份认证，但没有权限访问特定资源。

登录故障定位顺序：读取失败测试 test_expired_credential_returns_401 的输出，读取 demo_service/auth.py 的 login 函数，检查 ExpiredTokenError 是否被正确捕获，再核对 PR 的 diff 和 commit SHA。

## 金额规则

币种是 CNY，金额使用 Decimal、保留两位小数、采用 ROUND_HALF_UP。单价 2.675 元且数量 1 时，合计应为 2.68 元。单价 2.685 元且数量 1 时，合计应为 2.69 元。

## 库存规则

单次购买上限是 5 件。数量必须是正整数。超过 5 件时返回业务错误，不扣减库存。库存扣减属于 feature/inventory-guard PR 的测试内容。

## 发布与回滚

本演示项目的维护窗口为每周三 20:00 至 20:30，时区 Asia/Shanghai。回滚演练目标 RTO 是 10 分钟，备份演练目标 RPO 是 5 分钟。发布前必须通过 CI、检查未解决的审查意见、人工确认发布决定。

## 独立 CI 故障

Deliberate failure lab 工作流的 missing-dependency 会产生 ModuleNotFoundError，缺少的虚构模块叫 lab_optional_gateway。simulated-timeout 会产生 TimeoutError；它没有发起真实网络请求。两者与登录 PR 没有因果关系。

## 证据边界

本资料没有公司真实营收、个人手机号、真实密钥或合同编号。被问到这些内容时，应说明资料不足，不得编造。先前的规则若与本文件或 ADR-0001 不一致，应明确指出冲突并以当前 Accepted 规则为依据。
