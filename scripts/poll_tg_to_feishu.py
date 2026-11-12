import asyncio
import base64
import hashlib
import hmac
import json
import os
import re
import time
from datetime import date, datetime
from pathlib import Path

import requests
from telethon import TelegramClient, utils
from telethon.errors import RPCError
from telethon.sessions import StringSession

from message_digest import (
    DIGEST_SECONDS, SHANGHAI, Record, ingest, merge_records, prepare_state,
    priority_labels, render_pages,
)

STATE_PATH = Path("state/last_ids.json")
BATCH_LIMIT = int(os.getenv("BATCH_LIMIT", "200"))
MAX_TEXT_LENGTH = int(os.getenv("MAX_TEXT_LENGTH", "3500"))
FORWARD_INITIAL = os.getenv("FORWARD_INITIAL", "0") == "1"
LAST_SEND = 0.0
DEFAULT_TOKEN_EXPIRY = "2027-01-26"
CONTROL_KEY = "__control__"


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
    temporary = STATE_PATH.with_suffix(".tmp")
    temporary.write_text(
        json.dumps(state, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    temporary.replace(STATE_PATH)


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


def feishu_text_payload(text):
    return {
        "msg_type": "text",
        "content": {"text": text},
    }


def send_feishu(payload, fallback_text):
    global LAST_SEND
    webhook = required_env("FEISHU_WEBHOOK")
    secret = os.getenv("FEISHU_SECRET", "").strip()

    if secret:
        timestamp = str(int(time.time()))
        payload["timestamp"] = timestamp
        payload["sign"] = feishu_sign(timestamp, secret)

    time.sleep(max(0, 0.8 - (time.monotonic() - LAST_SEND)))
    LAST_SEND = time.monotonic()
    response = requests.post(webhook, json=payload, timeout=15)
    response.raise_for_status()
    data = response.json()
    code = data.get("code", data.get("StatusCode"))
    if code != 0:
        if payload.get("msg_type") == "interactive":
            print(f"::warning::Feishu card rejected (code={code}); sending plain text.")
            for offset in range(0, len(fallback_text), MAX_TEXT_LENGTH):
                chunk = fallback_text[offset:offset + MAX_TEXT_LENGTH]
                send_feishu(feishu_text_payload(chunk), chunk)
            return
        raise RuntimeError(f"Feishu webhook failed (code={code})")


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
        if isinstance(chat_ref, int) and utils.get_peer_id(entity) == chat_ref:
            return entity
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


async def initial_latest_id(client, entity):
    messages = await client.get_messages(entity, limit=1)
    return messages[0].id if messages else 0


async def find_control_group(client, ref):
    wanted = normalize(ref)
    matches = {}
    async for dialog in client.iter_dialogs():
        if not dialog.is_group:
            continue
        entity = dialog.entity
        peer_id = utils.get_peer_id(entity)
        if str(peer_id) == ref or wanted in {
            normalize(dialog.name), normalize(getattr(entity, "username", None))
        }:
            matches[peer_id] = entity
    if len(matches) != 1:
        raise ValueError("ambiguous" if matches else "not_found")
    return next(iter(matches.items()))


def notify_control(text):
    send_feishu(feishu_text_payload(text), text)


async def process_saved_commands(client, state, configured_refs):
    control = state.setdefault(CONTROL_KEY, {})
    if "cursor" not in control:
        control["cursor"] = await initial_latest_id(client, "me")
        save_state(state)
        return

    async for message in client.iter_messages(
        "me", min_id=control["cursor"], reverse=True, limit=100
    ):
        text = (message.raw_text or "").strip()
        is_command = getattr(message, "out", False) and not getattr(message, "fwd_from", None)
        add = re.fullmatch(r"添加转发\s+([^\n]{1,200})", text) if is_command else None
        expiry = re.fullmatch(r"更新令牌到期日\s+(\d{4}-\d{2}-\d{2})", text) if is_command else None
        notice = None
        if add:
            ref = add.group(1).strip()
            try:
                peer_id, entity = await find_control_group(client, ref)
            except ValueError as error:
                reason = "有多个同名群，请改用群 ID" if str(error) == "ambiguous" else "未找到该群，请检查名称或是否已加入"
                notice = f"转发群添加失败：{ref}。{reason}。"
            else:
                managed = control.setdefault("managed_groups", [])
                title = entity_title(entity, ref)
                existing = str(peer_id) in {str(value) for value in configured_refs} or any(
                    normalize(value) == normalize(title) for value in configured_refs
                    if isinstance(value, str)
                )
                if peer_id in managed or existing:
                    notice = f"转发群已存在：{title}。"
                else:
                    latest = await initial_latest_id(client, entity)
                    state[str(peer_id)] = {"title": title, "last_id": latest, "initialized": True}
                    managed.append(peer_id)
                    save_state(state)
                    notice = f"已添加转发群：{title}。从现在的新消息开始转发。"
        elif expiry:
            try:
                new_date = date.fromisoformat(expiry.group(1))
                if new_date < datetime.now(SHANGHAI).date():
                    raise ValueError("past")
            except ValueError:
                notice = "令牌到期日无效，请使用未来日期，格式为 YYYY-MM-DD。"
            else:
                control["token_expiry"] = new_date.isoformat()
                control.pop("last_expiry_alert", None)
                save_state(state)
                notice = f"令牌到期提醒已更新：{new_date.isoformat()}。"
        if notice:
            notify_control(notice)
        control["cursor"] = message.id
        save_state(state)


def warn_token_expiry(state, today=None):
    control = state.setdefault(CONTROL_KEY, {})
    today = today or datetime.now(SHANGHAI).date()
    expiry = date.fromisoformat(control.get("token_expiry", DEFAULT_TOKEN_EXPIRY))
    days = (expiry - today).days
    alert_key = f"{expiry.isoformat()}:{today.isoformat()}"
    if days > 7 or control.get("last_expiry_alert") == alert_key:
        return
    when = f"还有 {days} 天" if days >= 0 else "已过期"
    text = f"GitHub 定时触发令牌{when}（到期日 {expiry.isoformat()}），请及时更新 cron-job.org 中的令牌。"
    notify_control(text)
    control["last_expiry_alert"] = alert_key
    save_state(state)


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


async def as_record(entity, message, previous_id=None):
    body = message.raw_text or ("[非文本消息，请查看 Telegram 原消息]" if message.media else "[空消息]")
    return Record(
        id=message.id,
        sender_id=message.sender_id,
        timestamp=message.date.timestamp(),
        text=body,
        sender=await sender_name(message),
        reply_to=message.reply_to_msg_id,
        previous_id=previous_id,
        media=bool(message.media),
        link=build_message_link(entity, message.id),
    )


async def process_chat(client, chat_ref, state, now=None):
    now = time.time() if now is None else now
    entity = await resolve_entity(client, chat_ref)
    peer_key = str(utils.get_peer_id(entity))
    title = entity_title(entity, chat_ref)
    previous = state.setdefault(peer_key, {"title": title, "last_id": 0})
    last_id = int(previous.get("last_id", 0))

    if last_id == 0 and not previous.get("initialized") and not FORWARD_INITIAL:
        latest_id = await initial_latest_id(client, entity)
        state[peer_key] = {"title": title, "last_id": latest_id, "initialized": True}
        print(f"Initialized {title} at message {latest_id}; historical messages were not forwarded.")
        return

    prepare_state(previous, now)
    previous["title"] = title
    fresh = await fetch_messages(client, entity, last_id)
    records = [await as_record(entity, message) for message in fresh]
    ingest(previous, records)
    # Persist only IDs and fingerprints. Message text stays in Telegram, not Git.
    save_state(state)
    by_id = {record.id: record for record in records}
    missing = [entry["id"] for entry in previous["pending"] if entry["id"] not in by_id]
    for offset in range(0, len(missing), 100):
        ids = missing[offset:offset + 100]
        loaded = await client.get_messages(entity, ids=ids)
        for message_id, message in zip(ids, loaded):
            if message is None:
                by_id[message_id] = Record(message_id, None, now, "[原消息已删除或不可读取]", "unknown")
            else:
                by_id[message_id] = await as_record(entity, message)
    pending = []
    for entry in previous["pending"]:
        record = by_id[entry["id"]]
        record.previous_id = entry["previous_id"]
        pending.append(record)
    blocks = merge_records(pending)
    priority = []
    ordinary = []
    for block in blocks:
        target = priority if priority_labels("\n".join(item.text for item in block)) else ordinary
        target.append(block)

    counts = {"priority": 0, "digest": 0}

    def deliver(pages, kind):
        for payload, sent_ids, fallback in pages:
            send_feishu(payload, fallback)
            sent = set(sent_ids)
            previous["pending"] = [entry for entry in previous["pending"] if entry["id"] not in sent]
            save_state(state)
            counts[kind] += 1

    deliver(render_pages(title, priority, priority=True), "priority")
    slot = int(now // DIGEST_SECONDS)
    if slot > previous["digest_slot"]:
        deliver(render_pages(title, ordinary, duplicates=previous["duplicates"]), "digest")
        previous["digest_slot"] = slot
        previous["duplicates"] = 0
        save_state(state)
    print(
        f"{title}: priority_cards={counts['priority']}, digest_cards={counts['digest']}, "
        f"pending={len(previous['pending'])}, duplicates={previous['duplicates']}, last_id={previous['last_id']}"
    )


async def main():
    api_id = int(required_env("TG_API_ID"))
    api_hash = required_env("TG_API_HASH")
    session = required_env("TG_STRING_SESSION")
    chat_refs = parse_chat_refs()
    state = load_state()
    failures = []
    async with TelegramClient(StringSession(session), api_id, api_hash) as client:
        try:
            await process_saved_commands(client, state, chat_refs)
        except Exception as error:
            failures.append(type(error).__name__)
            print(f"::error::Saved Messages processing failed ({type(error).__name__}).")
        try:
            warn_token_expiry(state)
        except Exception as error:
            failures.append(type(error).__name__)
            print(f"::error::Token expiry reminder failed ({type(error).__name__}).")
        managed = state.get(CONTROL_KEY, {}).get("managed_groups", [])
        for chat_ref in list(dict.fromkeys(chat_refs + managed)):
            try:
                await process_chat(client, chat_ref, state)
            except Exception as error:
                failures.append(type(error).__name__)
                print(f"::error::Chat processing failed ({type(error).__name__}); queued messages retained.")
            finally:
                save_state(state)

    if failures:
        raise RuntimeError(f"{len(failures)} chat(s) failed; pending messages will be retried next run")


if __name__ == "__main__":
    asyncio.run(main())
