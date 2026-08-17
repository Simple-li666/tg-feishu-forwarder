import asyncio
import base64
import hashlib
import hmac
import json
import os
import time
from pathlib import Path

import requests
from telethon import TelegramClient, utils
from telethon.errors import RPCError
from telethon.sessions import StringSession


STATE_PATH = Path("state/last_ids.json")
BATCH_LIMIT = int(os.getenv("BATCH_LIMIT", "50"))
MAX_TEXT_LENGTH = int(os.getenv("MAX_TEXT_LENGTH", "3500"))
FORWARD_INITIAL = os.getenv("FORWARD_INITIAL", "0") == "1"


def required_env(name):
    value = os.getenv(name)
    if not value:
        raise RuntimeError(f"Missing required env: {name}")
    return value


def load_state():
    if not STATE_PATH.exists():
        return {}
    raw = STATE_PATH.read_text(encoding="utf-8").strip()
    if not raw:
        return {}
    return json.loads(raw)


def save_state(state):
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(
        json.dumps(state, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def parse_chat_refs():
    raw = required_env("TG_CHAT_IDS")
    separator = ";" if ";" in raw else ","
    refs = []
    for item in raw.split(separator):
        ref = item.strip()
        if not ref:
            continue
        refs.append(int(ref) if ref.lstrip("-").isdigit() else ref)
    if not refs:
        raise RuntimeError("TG_CHAT_IDS is empty")
    return refs


def feishu_sign(timestamp, secret):
    string_to_sign = f"{timestamp}\n{secret}"
    digest = hmac.new(
        string_to_sign.encode("utf-8"),
        digestmod=hashlib.sha256,
    ).digest()
    return base64.b64encode(digest).decode("utf-8")


def truncate_text(text):
    if len(text) <= MAX_TEXT_LENGTH:
        return text
    return text[: MAX_TEXT_LENGTH - 20] + "\n...[内容过长已截断]"


def feishu_text_payload(text):
    return {
        "msg_type": "text",
        "content": {"text": truncate_text(text)},
    }


def escape_markdown(text):
    escaped = str(text).replace("\\", "\\\\")
    for char in "`*_{}[]()#+-.!|>~":
        escaped = escaped.replace(char, f"\\{char}")
    return escaped


def markdown_element(content):
    return {
        "tag": "markdown",
        "content": content,
        "text_align": "left",
        "text_size": "normal_v2",
        "margin": "0px 0px 0px 0px",
    }


def build_card(title, sender, message_id, body, link):
    metadata = (
        f"群组： **{escape_markdown(title)}**\n"
        f"发送者： {escape_markdown(sender)}\n"
        f"消息ID： {message_id}"
    )
    body_markdown = "\n".join(
        f"**{escape_markdown(line or ' ')}**"
        for line in truncate_text(body).splitlines() or [""]
    )
    elements = [markdown_element(metadata), markdown_element(body_markdown)]

    if link:
        elements.append(markdown_element(f"[链接：{link}]({link})"))

    return {
        "schema": "2.0",
        "config": {"update_multi": True},
        "body": {
            "direction": "vertical",
            "padding": "12px 12px 12px 12px",
            "elements": elements,
        },
        "header": {
            "title": {"tag": "plain_text", "content": "TG -> 飞书"},
            "template": "blue",
            "padding": "12px 12px 12px 12px",
        },
    }


def feishu_card_payload(title, sender, message_id, body, link):
    return {
        "msg_type": "interactive",
        "card": build_card(title, sender, message_id, body, link),
    }


def send_feishu(payload, fallback_text):
    webhook = required_env("FEISHU_WEBHOOK")
    secret = os.getenv("FEISHU_SECRET", "").strip()

    if secret:
        timestamp = str(int(time.time()))
        payload["timestamp"] = timestamp
        payload["sign"] = feishu_sign(timestamp, secret)

    response = requests.post(webhook, json=payload, timeout=15)
    response.raise_for_status()
    data = response.json()
    code = data.get("code", data.get("StatusCode", 0))
    if code != 0:
        if payload.get("msg_type") == "interactive":
            send_feishu(feishu_text_payload(fallback_text), fallback_text)
            return
        raise RuntimeError(f"Feishu webhook failed: {data}")


def entity_title(entity, fallback):
    return (
        getattr(entity, "title", None)
        or getattr(entity, "username", None)
        or getattr(entity, "first_name", None)
        or str(fallback)
    )


def normalize(value):
    return str(value or "").strip().lstrip("@").casefold()


async def resolve_entity(client, chat_ref):
    try:
        return await client.get_entity(chat_ref)
    except (ValueError, TypeError, RPCError):
        pass

    wanted = normalize(chat_ref)
    async for dialog in client.iter_dialogs():
        entity = dialog.entity
        candidates = [
            dialog.name,
            getattr(entity, "title", None),
            getattr(entity, "username", None),
        ]
        if any(normalize(candidate) == wanted for candidate in candidates):
            return entity

    raise RuntimeError(
        f"Cannot find Telegram chat {chat_ref!r}. Run scripts/list_dialogs.py locally "
        "and use the numeric peer id if the title is duplicated or private."
    )


def build_message_link(entity, message_id):
    username = getattr(entity, "username", None)
    if username:
        return f"https://t.me/{username}/{message_id}"

    peer_id = str(utils.get_peer_id(entity))
    if peer_id.startswith("-100"):
        return f"https://t.me/c/{peer_id[4:]}/{message_id}"
    return ""


async def sender_name(message):
    sender = await message.get_sender()
    if not sender:
        return "unknown"

    title = getattr(sender, "title", None)
    if title:
        return title

    first = getattr(sender, "first_name", None)
    last = getattr(sender, "last_name", None)
    username = getattr(sender, "username", None)
    full_name = " ".join(part for part in [first, last] if part)
    return full_name or (f"@{username}" if username else str(getattr(sender, "id", "unknown")))


async def format_message(entity, message):
    title = entity_title(entity, "unknown")
    sender = await sender_name(message)
    body = message.raw_text

    if not body:
        if message.media:
            body = "[非文本消息，可能是图片、文件、语音、贴纸或投票]"
        else:
            body = "[空消息]"

    link = build_message_link(entity, message.id)
    link_line = f"\n链接：{link}" if link else ""

    fallback_text = (
        "[TG -> 飞书]\n"
        f"群组：{title}\n"
        f"发送者：{sender}\n"
        f"消息ID：{message.id}\n\n"
        f"{body}"
        f"{link_line}"
    )
    payload = feishu_card_payload(title, sender, message.id, body, link)
    return payload, fallback_text


async def initial_latest_id(client, entity):
    messages = await client.get_messages(entity, limit=1)
    return messages[0].id if messages else 0


async def fetch_messages(client, entity, last_id):
    if last_id == 0:
        messages = await client.get_messages(entity, limit=BATCH_LIMIT)
        return list(reversed(messages))

    messages = []
    async for message in client.iter_messages(
        entity,
        min_id=last_id,
        reverse=True,
        limit=BATCH_LIMIT,
    ):
        messages.append(message)
    return messages


async def process_chat(client, chat_ref, state):
    entity = await resolve_entity(client, chat_ref)
    peer_key = str(utils.get_peer_id(entity))
    title = entity_title(entity, chat_ref)
    previous = state.get(peer_key, {})
    last_id = int(previous.get("last_id", 0))

    if last_id == 0 and not FORWARD_INITIAL:
        latest_id = await initial_latest_id(client, entity)
        state[peer_key] = {"title": title, "last_id": latest_id}
        print(f"Initialized {title} at message {latest_id}; historical messages were not forwarded.")
        return

    max_id = last_id
    sent = 0
    for message in await fetch_messages(client, entity, last_id):
        payload, fallback_text = await format_message(entity, message)
        send_feishu(payload, fallback_text)
        max_id = max(max_id, message.id)
        sent += 1

    state[peer_key] = {"title": title, "last_id": max_id}
    print(f"{title}: forwarded {sent} message(s), last_id={max_id}")


async def main():
    api_id = int(required_env("TG_API_ID"))
    api_hash = required_env("TG_API_HASH")
    session = required_env("TG_STRING_SESSION")
    chat_refs = parse_chat_refs()
    state = load_state()

    async with TelegramClient(StringSession(session), api_id, api_hash) as client:
        for chat_ref in chat_refs:
            await process_chat(client, chat_ref, state)

    save_state(state)


if __name__ == "__main__":
    asyncio.run(main())
