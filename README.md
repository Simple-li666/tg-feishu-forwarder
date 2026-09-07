# Telegram 群消息转发到飞书

这个仓库用 GitHub Actions 轮询 Telegram 群消息，并通过飞书自定义机器人转发到飞书群。触发方式由外部 HTTP cron 服务调用 GitHub `workflow_dispatch` API。

适合场景：Telegram 群不能加机器人，但你的个人账号能看到群消息；你接受 5 到 15 分钟级别延迟。

## 文件说明

```text
.github/workflows/tg-to-feishu.yml  # GitHub Actions 转发任务
EXTERNAL_CRON_SETUP.md              # cron-job.org 每 5 分钟触发配置
scripts/make_session.py             # 本地生成 Telegram StringSession
scripts/list_dialogs.py             # 本地列出可见群组和 ID
scripts/poll_tg_to_feishu.py        # Actions 实际运行的转发脚本
scripts/message_digest.py          # 去重、合并、需求规则和汇总卡片
state/last_ids.json                 # 读取游标、待发送 ID、去重指纹和汇总时间
tests/test_message_digest.py        # 规则与跨次运行验证
```

## GitHub Secrets

需要在仓库的 `Settings -> Secrets and variables -> Actions` 里配置：

```text
TG_API_ID
TG_API_HASH
TG_STRING_SESSION
TG_CHAT_IDS
FEISHU_WEBHOOK
FEISHU_SECRET
```

`FEISHU_SECRET` 只有飞书机器人开启签名校验时才需要。

`TG_CHAT_IDS` 支持数字群 ID、公开群用户名，也支持当前账号可见的群标题，例如：

```text
某个群标题
```

如果群名重复或解析失败，先本地运行 `scripts/list_dialogs.py`，再改用类似 `-1001234567890` 的数字 ID。

多个群可以用英文分号分隔；没有分号时兼容旧的英文逗号分隔方式。群名本身含逗号时必须使用分号分隔，长期配置建议使用数字 ID。

## 消息降噪和优先提醒

仍由 cron-job.org 每 5 分钟触发一次，所有已配置群组统一应用以下规则：

- 普通消息按群合并为 15 分钟汇总卡片，在每小时的 00、15、30、45 分钟后的首个成功轮询发送；没有消息不发空汇总。定时服务延迟会顺延发送时间。
- 采购、询价、充值需求在下一次成功轮询中优先推送，不等汇总窗口。采用本地规则初筛，卡片展示匹配类别。典型表达包括“需要 AWS 账号”“GCP 什么价格”“帮我充值”“余额不足，需要续费”。明显供应广告进入普通汇总；含糊表达可能误判，尚未接入 AI。
- 同群、同发送者、同回复目标，30 分钟内完全相同的较长文本（归一化空白后至少 12 字符）只转发首次，汇总显示去重数量。不同发送者、不同群、短回复、带媒体的消息分别保留。
- 同群同发送者的相邻发言，间隔不超过 2 分钟且回复目标一致时合并。每段最多 50 条、总跨度不超过 10 分钟。整段命中需求规则时一起优先发送。普通消息可跨轮询合并；已发送的优先提醒不会等待后续发言。
- 卡片保留群名、发送者、北京时间、消息 ID 和可用的原消息链接。群名和正文加粗。过长内容分卡发送，不静默截断；汇总是原文整理，不生成 AI 摘要。

读取游标与待发送队列保存在同一个状态文件中。队列只保存消息 ID 和前一条消息 ID；去重记录只保存 SHA-256 指纹和时间，正文、发送者名称不写入状态文件。汇总时重新从 Telegram 读取原文；已删除或无法按 ID 读取的消息显示占位说明。

发送成功后才移除对应队列条目。单群失败后继续处理其他群，工作流即使失败也尝试提交已完成进度，下一次重试未完成内容。飞书接收成功但网络回包丢失、进程中断或 Git 状态提交失败时，仍可能出现重发；Webhook 没有可用的端到端幂等确认，不能保证绝对只发一次。

`BATCH_LIMIT` 当前为每群每次最多读取 200 条新消息；超出部分在后续轮询继续读取，不跳过。持续超量时会积压，优先提醒也可能延后。队列通过 Git 持久化，不依赖 Actions runner 留存。

本地验证：

```bash
python -m unittest discover -s tests -v
```

## 生成 TG_STRING_SESSION

这个步骤只需要做一次，并且必须在能连接 Telegram 的环境运行：

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
export TG_API_ID="你的 api_id"
export TG_API_HASH="你的 api_hash"
python scripts/make_session.py
```

按提示输入 Telegram 手机号、验证码和两步验证密码。脚本最后会输出一长串 `StringSession`，把它保存到 GitHub Secret `TG_STRING_SESSION`。

不要把 `TG_STRING_SESSION` 提交到仓库。

## 首次运行

第一次运行 workflow 默认只初始化游标，不转发历史消息，避免刷屏。初始化后，在 Telegram 群里发一条新消息，再手动运行一次 workflow，或等 cron-job.org 下一次触发，就应该能在飞书收到。

要手动运行：

```text
Actions -> TG to Feishu -> Run workflow
```

## 每 5 分钟自动触发

GitHub 自带的 `schedule` 触发可能延迟或不触发，所以这里使用 cron-job.org 每 5 分钟调用 GitHub `workflow_dispatch` API。

配置步骤见：

```text
EXTERNAL_CRON_SETUP.md
```

## 安全建议

- 仓库保持私有。
- Telegram 开启两步验证。
- 不要在 issue、README、commit 或 Actions 日志里打印 `TG_STRING_SESSION`、`TG_API_HASH`、飞书 webhook。
- cron-job.org 里的 GitHub token 只给这个仓库的 `Actions: Read and write` 权限。
- 如果密钥曾经出现在公开聊天或截图里，建议轮换 Telegram app 的 `api_hash` 和飞书 webhook。
