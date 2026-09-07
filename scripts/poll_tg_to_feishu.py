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

from message_digest import (
    DIGEST_SECONDS, Record, ingest, merge_records, prepare_state,
    priority_labels, render_pages,
)

STATE_PATH = Path("state/last_ids.json")
BATCH_LIMIT = int(os.getenv("BATCH_LIMIT", "200"))
MAX_TEXT_LENGTH = int(os.getenv("MAX_TEXT_LENGTH", "3500"))
FORWARD_INITIAL = os.getenv("FORWARD_INITIAL", "0") == "1"
LAST_SEND = 0.0


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

    if last_id == 0 and not FORWARD_INITIAL:
        latest_id = await initial_latest_id(client, entity)
        state[peer_key] = {"title": title, "last_id": latest_id}
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
        for chat_ref in chat_refs:
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
