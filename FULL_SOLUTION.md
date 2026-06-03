# Telegram 群消息转发到飞书完整方案

## 目标

在中国大陆设备不连接 VPN 的情况下，通过飞书收到 Telegram 群里的新消息。

因为目标 Telegram 群不能添加机器人，所以本方案使用你的 Telegram 个人账号会话读取消息，再通过 GitHub Actions 和飞书自定义机器人完成转发。

## 最终架构

```text
cron-job.org 每 5 分钟触发
  -> GitHub workflow_dispatch API
  -> GitHub Actions 运行 Python 脚本
  -> Telethon 使用 TG_STRING_SESSION 读取 Telegram 群新消息
  -> 飞书自定义机器人 webhook 发到飞书群
  -> state/last_ids.json 记录游标，避免重复转发
```

## 为什么不用 GitHub 原生 schedule

GitHub Actions 的 `schedule` 触发可能延迟、拥堵时被跳过，实际测试中没有稳定出现 scheduled run。手动触发 `workflow_dispatch` 已经验证能正常转发，所以改成由 cron-job.org 每 5 分钟调用 GitHub API 触发 workflow。

这样链路更清楚：

```text
定时器是否触发：看 cron-job.org 执行日志
workflow 是否启动：看 GitHub Actions run
消息是否转发：看飞书群
```

## 仓库文件

```text
.github/workflows/tg-to-feishu.yml  # GitHub Actions 转发任务，只保留 workflow_dispatch
scripts/make_session.py             # 本地生成 Telegram StringSession
scripts/list_dialogs.py             # 本地列出可见 Telegram 群组和 ID
scripts/poll_tg_to_feishu.py        # Actions 实际运行的轮询转发脚本
state/last_ids.json                 # 去重游标，只记录最后消息 ID
EXTERNAL_CRON_SETUP.md              # cron-job.org 配置速查
FULL_SOLUTION.md                    # 本完整方案文档
```

## GitHub Actions 触发方式

当前 workflow 只接受手动/API 触发：

```yaml
on:
  workflow_dispatch:
    inputs:
      trigger_source:
        description: "Who triggered this run"
        required: false
        default: "manual"
```

cron-job.org 触发时会传：

```json
{
  "ref": "main",
  "inputs": {
    "trigger_source": "cron-job.org"
  }
}
```

Actions 日志会显示：

```text
Triggered by cron-job.org
```

## GitHub Secrets

仓库需要配置这些 Actions Secrets：

```text
TG_API_ID
TG_API_HASH
TG_STRING_SESSION
TG_CHAT_IDS
FEISHU_WEBHOOK
FEISHU_SECRET
```

说明：

- `TG_API_ID` / `TG_API_HASH`：来自 Telegram API app。
- `TG_STRING_SESSION`：通过 `scripts/make_session.py` 登录 Telegram 个人账号生成。
- `TG_CHAT_IDS`：目标 Telegram 群 ID、公开群用户名，或当前账号可见的群标题。
- `FEISHU_WEBHOOK`：飞书自定义机器人 webhook。
- `FEISHU_SECRET`：飞书机器人签名密钥，如果没有开启签名校验可以为空。

## Telegram 消息读取逻辑

脚本使用 Telethon 读取你的 Telegram 账号可见的群消息。

第一次运行某个群时：

```text
只初始化 state/last_ids.json，不转发历史消息
```

后续运行：

```text
只转发 message_id 大于 last_id 的新消息
```

如果群名匹配失败，运行：

```bash
python scripts/list_dialogs.py
```

然后把 `TG_CHAT_IDS` 改成数字 ID，例如：

```text
-1001234567890
```

## cron-job.org 配置

创建一个新的 cron job。

基础配置：

```text
Title: tg-feishu-forwarder
URL: https://api.github.com/repos/Simple-li666/tg-feishu-forwarder/actions/workflows/tg-to-feishu.yml/dispatches
Schedule: Every 5 minutes
Request method: POST
```

Headers：

```text
Accept: application/vnd.github+json
Authorization: Bearer YOUR_GITHUB_TOKEN
X-GitHub-Api-Version: 2026-03-10
Content-Type: application/json
```

Body：

```json
{
  "ref": "main",
  "inputs": {
    "trigger_source": "cron-job.org"
  }
}
```

成功判断：

```text
HTTP 204 No Content
```

如果 cron-job.org 需要设置期望状态码，填 `204` 或接受所有 `2xx`。

## GitHub Fine-Grained PAT 权限

为了让 cron-job.org 能触发 workflow，需要创建一个 GitHub fine-grained personal access token。

推荐设置：

```text
Token name: cron-job tg-feishu-forwarder
Expiration: 30 days
Resource owner: Simple-li666
Repository access: Only select repositories
Selected repository: Simple-li666/tg-feishu-forwarder
Repository permissions:
  Actions: Read and write
  Metadata: Read-only, required by GitHub
Account permissions: none
```

这个 token 只用于调用：

```text
POST /repos/Simple-li666/tg-feishu-forwarder/actions/workflows/tg-to-feishu.yml/dispatches
```

不要把 token 写入仓库、聊天记录、README 或 Actions 日志。

## 验证步骤

1. 在 cron-job.org 保存任务后，点一次手动运行或测试。
2. 打开 GitHub Actions，确认出现新的 `TG to Feishu` run。
3. 查看该 run 的 `Show trigger source` 步骤，应该看到：

```text
Triggered by cron-job.org
```

4. 在 Telegram 群里发一条新消息。
5. 等 cron-job.org 下一轮 5 分钟触发。
6. 飞书群应该收到转发消息。
7. GitHub 仓库的 `state/last_ids.json` 会被 Actions 自动提交更新。

## 排错

### cron-job.org 显示 401

GitHub token 错误、过期、未复制完整，或没有 `Actions: Read and write` 权限。

### cron-job.org 显示 404

常见原因：

- token 没有这个仓库的访问权限。
- workflow 文件名写错。
- URL 路径写错。

正确 URL：

```text
https://api.github.com/repos/Simple-li666/tg-feishu-forwarder/actions/workflows/tg-to-feishu.yml/dispatches
```

### cron-job.org 显示 422

常见原因：

- body 不是合法 JSON。
- `ref` 不是 `main`。
- workflow 没有 `workflow_dispatch`。

### GitHub Actions 成功，但飞书没有消息

检查：

- Telegram 群里是否确实有高于 `state/last_ids.json` 的新消息。
- Actions 日志里 `Poll Telegram and send to Feishu` 是否显示 forwarded message。
- 飞书 webhook 是否被禁用或机器人是否仍在群里。

### 重复转发

检查 `Commit state` 步骤是否成功。它需要 workflow 权限：

```yaml
permissions:
  contents: write
```

## 安全边界

- `TG_STRING_SESSION` 等同于 Telegram 登录凭证，必须只放 GitHub Secrets。
- GitHub PAT 只放 cron-job.org 的请求 Header。
- PAT 设置 30 天过期，过期后重新生成并替换 cron-job.org Header。
- 如果怀疑泄露，立刻撤销 GitHub PAT，并在 Telegram 客户端里结束可疑会话。

## 回滚方案

如果不想再自动触发：

1. 在 cron-job.org 禁用或删除该 job。
2. 可选：在 GitHub 删除对应 fine-grained PAT。
3. GitHub Actions 仍可手动运行，不影响仓库代码。
