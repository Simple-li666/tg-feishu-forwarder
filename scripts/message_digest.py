"""Queue metadata and deterministic rules for Telegram message digests."""

import hashlib
import html
import json
import re
from dataclasses import dataclass
from datetime import datetime
from zoneinfo import ZoneInfo


DIGEST_SECONDS = 900
MERGE_SECONDS = 120
DEDUP_SECONDS = 1800
MAX_CARD_BYTES = 18000
SHANGHAI = ZoneInfo("Asia/Shanghai")


@dataclass
class Record:
    id: int
    sender_id: int | None
    timestamp: float
    text: str
    sender: str = "unknown"
    reply_to: int | None = None
    previous_id: int | None = None
    media: bool = False
    link: str = ""


def priority_labels(text):
    compact = re.sub(r"\s+", "", text).casefold()
    buyer = re.search(r"求购|采购|想买|要买|收购|我需要|想充值|帮我|谁能|有没有|询价", compact)
    question = re.search(r"[?？]|多少钱|什么价|怎么收费|价格多少|什么折扣|多少折|怎么充", compact)
    seller = re.search(r"长期供应|长期提供|大量出售|现货|欢迎咨询|有需要.{0,4}联系|出(?:售|一批|账号|aws|gcp)|专业代充|承接", compact)
    if seller and not buyer and not question:
        return []
    labels = []
    if re.search(r"充值|代充|充钱|续费|余额不足|帮我充|怎么充", compact):
        if not re.search(r"(?:不需要|不用|无需|暂不)充值", compact):
            labels.append("充值")
    product = re.search(r"aws|gcp|腾讯|阿里|谷歌|google|云|账号|账户|服务器|机器|算力|代理|\bip\b|流量|渠道|套餐", text, re.I)
    purchase = re.search(r"求购|采购|想买|要买|收购", compact)
    if purchase or (product and re.search(r"需要|求一个|求个|收一个|收个|急需|求推荐|有没有|有.+吗", compact)):
        if not re.search(r"(?:不需要|不用|无需|暂不)(?:购买|采购|账号|服务器)", compact):
            labels.append("采购")
    if re.search(r"询价|多少钱|什么价|怎么收费|价格多少|报价多少|什么折扣|多少折|求报价|报个价", compact):
        labels.append("询价")
    elif product and re.search(r"报价|价格|折扣", compact) and question:
        labels.append("询价")
    return list(dict.fromkeys(labels))


def fingerprint(record):
    text = " ".join(record.text.split())
    # Short replies, media and different senders may represent distinct requests.
    if record.media or record.sender_id is None or len(text) < 12:
        return None
    value = json.dumps([record.sender_id, record.reply_to, text], ensure_ascii=False)
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def prepare_state(state, now):
    state.setdefault("pending", [])
    state.setdefault("recent", {})
    state.setdefault("duplicates", 0)
    state.setdefault("digest_slot", int(now // DIGEST_SECONDS))
    state["recent"] = {
        key: value for key, value in state["recent"].items()
        if value >= now - DEDUP_SECONDS
    }


def ingest(state, records):
    for record in sorted(records, key=lambda item: item.id):
        if record.id <= state["last_id"]:
            continue
        previous_id = state["last_id"]
        key = fingerprint(record)
        old = state["recent"].get(key) if key else None
        duplicate = old is not None and 0 <= record.timestamp - old <= DEDUP_SECONDS
        if duplicate:
            state["duplicates"] += 1
        else:
            state["pending"].append({"id": record.id, "previous_id": previous_id})
            if key:
                state["recent"][key] = record.timestamp
        state["last_id"] = record.id


def merge_records(records):
    blocks = []
    for record in records:
        previous = blocks[-1][-1] if blocks else None
        if (
            previous is not None
            and record.sender_id is not None
            and previous.sender_id == record.sender_id
            and record.previous_id == previous.id
            and record.reply_to == previous.reply_to
            and 0 <= record.timestamp - previous.timestamp <= MERGE_SECONDS
            and record.timestamp - blocks[-1][0].timestamp <= 600
            and len(blocks[-1]) < 50
        ):
            blocks[-1].append(record)
        else:
            blocks.append([record])
    return blocks


def escape_markdown(text):
    text = html.escape(str(text), quote=False)
    return re.sub(r"([\\`*_{}\[\]()#+.!|~>-])", r"\\\1", text)


def bold_text(text):
    return "\n".join(
        f"**{escape_markdown(line.strip())}**" if line.strip() else ""
        for line in text.splitlines()
    )


def card_payload(title, heading, content, priority):
    return {
        "msg_type": "interactive",
        "card": {
            "schema": "2.0",
            "config": {"update_multi": True},
            "header": {
                "title": {"tag": "plain_text", "content": heading},
                "template": "orange" if priority else "blue",
            },
            "body": {"elements": [
                {"tag": "markdown", "content": f"群组： **{escape_markdown(title)}**"},
                {"tag": "markdown", "content": content},
            ]},
        },
    }


def render_pages(title, blocks, priority=False, duplicates=0):
    """Each page acknowledges only the IDs fully represented on that page."""
    heading = "TG -> 飞书 | 优先提醒" if priority else "TG -> 飞书 | 15分钟汇总"
    pages = []
    content = []
    ids = []

    def flush():
        if content:
            body = "\n\n---\n\n".join(content)
            pages.append((card_payload(title, heading, body, priority), list(ids),
                          f"{heading}\n群组：{title}\n{body}"))
            content.clear()
            ids.clear()

    for block in blocks:
        labels = priority_labels("\n".join(record.text for record in block))
        start = datetime.fromtimestamp(block[0].timestamp, SHANGHAI).strftime("%m-%d %H:%M")
        sender = escape_markdown(block[0].sender)
        prefix = f"{start} · {sender} · {len(block)}条合并" if len(block) > 1 else f"{start} · {sender}"
        if priority:
            prefix += f"\n需求： **{' / '.join(labels)}**（规则初筛）"
        body = "\n".join(record.text for record in block)
        # A merged conversation is acknowledged only after its final fragment succeeds.
        fragments = [body[i:i + 1000] for i in range(0, len(body), 1000)] or ["[空消息]"]
        message_ids = ", ".join(str(record.id) for record in block)
        for index, fragment in enumerate(fragments):
            part = f"\n续 {index + 1}/{len(fragments)}" if len(fragments) > 1 else ""
            item = f"{prefix}{part}\n{bold_text(fragment)}\n消息ID：{message_ids}"
            if block[0].link:
                item += f" · [原消息]({block[0].link})"
            candidate = "\n\n---\n\n".join(content + [item])
            payload = card_payload(title, heading, candidate, priority)
            if len(json.dumps(payload, ensure_ascii=False).encode("utf-8")) > MAX_CARD_BYTES or len(content) >= 10:
                flush()
            content.append(item)
            if index == len(fragments) - 1:
                ids.extend(record.id for record in block)
    if duplicates and not priority:
        notice = f"本轮合并重复消息 {duplicates} 条。"
        candidate = card_payload(title, heading, "\n\n---\n\n".join(content + [notice]), priority)
        if len(json.dumps(candidate, ensure_ascii=False).encode("utf-8")) > MAX_CARD_BYTES:
            flush()
        content.append(notice)
    flush()
    return pages
