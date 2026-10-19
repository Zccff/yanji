# 研迹的 GitHub Token 配置

GitHub 没有真正不限流的个人 Token。按官方 REST API 规则，未认证请求通常为每 IP 每小时 60 次，个人 Token 通常为每账号每小时 5,000 次；同一账号的多个 Token 共享个人配额。搜索等接口还有独立限制，并存在二级限流。GitHub App 安装 Token 可以支持更大规模的集成，但仍有配额。

官方说明：[REST API 限流](https://docs.github.com/en/rest/using-the-rest-api/rate-limits-for-the-rest-api)。

## 在哪里创建

打开 [Fine-grained Token 创建页](https://github.com/settings/personal-access-tokens/new)，或依次进入 GitHub 头像 → Settings → Developer settings → Personal access tokens → Fine-grained tokens → Generate new token。

名称可以填「研迹」，选择有效期。自己的仓库可选择对应 Resource owner 和目标仓库；用于当前同步器时，按需要开放 Contents、Issues、Pull requests、Actions 的读取权限，Metadata 保持默认读取即可。需要评论、创建/关闭 Issue 等写回功能时，再给相应资源增加写权限。

他人的公开仓库可以用自己的 Token 读取公开数据。私有仓库要求账号本身已经有访问权限，并完成相应组织的授权；Token 不能创造账号原本没有的访问权。

官方步骤与权限说明：[管理个人访问令牌](https://docs.github.com/en/authentication/keeping-your-account-and-data-secure/managing-your-personal-access-tokens)。

## 在项目中配置

全局配置位置为项目根目录 `.env`，即与 `start.cmd` 同级的文件：

```dotenv
GITHUB_TOKEN=你刚生成的Token
```

保存后重启 API 和 Worker，新的进程才会加载配置。当前后端也读取 `backend/.env`；若两个文件都有同名设置，后者优先。

也可以在网页「代码仓库」的连接表单中填写 `Access Token`。同一仓库重新提交连接表单会更新已保存的仓库 Token；仓库专属 Token 优先于全局配置。只更新全局 Token 时，应确认仓库没有继续使用旧的专属 Token。

Token 填入本机配置或网页输入框即可，无需粘贴到聊天中。

## 已填 Token 仍然 403

`rate limit exceeded` 时看响应中的 `x-ratelimit-remaining`、`x-ratelimit-reset` 或 `retry-after`，按指定时间等待。不要通过同账号反复生成 Token 来尝试叠加配额。普通 403 也可能来自缺少权限或组织限制，需要结合响应内容判断。
