# Telegram 群消息转发到飞书

这个仓库用 GitHub Actions 定时轮询 Telegram 群消息，并通过飞书自定义机器人转发到飞书群。

适合场景：Telegram 群不能加机器人，但你的个人账号能看到群消息；你接受 5 到 15 分钟级别延迟。

## 文件说明

```text
.github/workflows/tg-to-feishu.yml  # 每 10 分钟运行一次
scripts/make_session.py             # 本地生成 Telegram StringSession
scripts/list_dialogs.py             # 本地列出可见群组和 ID
scripts/poll_tg_to_feishu.py        # Actions 实际运行的转发脚本
state/last_ids.json                 # 去重游标，只记录最后消息 ID
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

第一次运行 workflow 默认只初始化游标，不转发历史消息，避免刷屏。初始化后，在 Telegram 群里发一条新消息，再手动运行一次 workflow，就应该能在飞书收到。

要手动运行：

```text
Actions -> TG to Feishu -> Run workflow
```

## 安全建议

- 仓库保持私有。
- Telegram 开启两步验证。
- 不要在 issue、README、commit 或 Actions 日志里打印 `TG_STRING_SESSION`、`TG_API_HASH`、飞书 webhook。
- 如果密钥曾经出现在公开聊天或截图里，建议轮换 Telegram app 的 `api_hash` 和飞书 webhook。
