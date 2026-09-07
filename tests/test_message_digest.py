import json
from datetime import datetime, timezone
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import message_digest as digest
import poll_tg_to_feishu as poll


class RulesTests(unittest.TestCase):
    def test_buyer_intent_and_recharge(self):
        cases = {
            "需要一个 AWS 新加坡账号": "采购",
            "有腾讯云账号吗": "采购",
            "GCP 什么价格？": "询价",
            "报价多少": "询价",
            "麻烦帮我充值1000元": "充值",
            "余额不足，需要续费": "充值",
        }
        for text, expected in cases.items():
            with self.subTest(text=text):
                self.assertIn(expected, digest.priority_labels(text))

    def test_ads_and_chat_do_not_become_priority(self):
        for text in ["长期供应 AWS 账号，欢迎咨询", "专业代充，长期提供充值服务", "好的", "收到", "今天一起吃饭", "暂不需要充值"]:
            with self.subTest(text=text):
                self.assertEqual([], digest.priority_labels(text))

    def test_duplicate_scope_and_expiry(self):
        state = {"last_id": 1}
        digest.prepare_state(state, 10000)
        text = "AWS 新加坡服务器现货，长期提供服务欢迎咨询"
        records = [
            digest.Record(2, 7, 10000, text),
            digest.Record(3, 7, 10010, text),
            digest.Record(4, 8, 10020, text),
            digest.Record(5, 7, 12000, text),
        ]
        digest.ingest(state, records)
        self.assertEqual([2, 4, 5], [x["id"] for x in state["pending"]])
        self.assertEqual(1, state["duplicates"])
        self.assertNotIn(text, json.dumps(state, ensure_ascii=False))
        other_chat = {"last_id": 1}
        digest.prepare_state(other_chat, 10000)
        digest.ingest(other_chat, [records[0]])
        self.assertEqual(1, len(other_chat["pending"]))

    def test_short_replies_and_media_are_not_content_deduplicated(self):
        state = {"last_id": 0}
        digest.prepare_state(state, 100)
        digest.ingest(state, [digest.Record(1, 7, 100, "好的"), digest.Record(2, 7, 101, "好的"),
                              digest.Record(3, 7, 102, "[同样的文件说明文字但内容不同]", media=True),
                              digest.Record(4, 7, 103, "[同样的文件说明文字但内容不同]", media=True)])
        self.assertEqual(4, len(state["pending"]))

    def test_merge_requires_adjacency_sender_time_and_reply(self):
        records = [digest.Record(1, 7, 100, "需要 AWS", previous_id=0),
                   digest.Record(2, 7, 160, "预算多少钱", previous_id=1),
                   digest.Record(3, 8, 170, "有", previous_id=2),
                   digest.Record(4, 7, 180, "私聊", previous_id=3),
                   digest.Record(5, 7, 400, "晚点", previous_id=4),
                   digest.Record(7, 7, 410, "跳过了别人的发言", previous_id=6),
                   digest.Record(8, 7, 420, "回复另一个主题", previous_id=7, reply_to=100)]
        blocks = digest.merge_records(records)
        self.assertEqual([[1, 2], [3], [4], [5], [7], [8]], [[m.id for m in b] for b in blocks])
        self.assertIn("询价", digest.priority_labels("\n".join(m.text for m in blocks[0])))

    def test_cards_page_without_truncating_or_acknowledging_early(self):
        raw = "充值说明_含中文和符号*<>!" * 1800
        record = digest.Record(1, 7, 100, raw, sender="测试 <at id=all>")
        pages = digest.render_pages("测试群", [[record]], priority=True)
        self.assertGreater(len(pages), 1)
        self.assertTrue(all(not ids for _, ids, _ in pages[:-1]))
        self.assertEqual([1], pages[-1][1])
        rendered = "".join(json.dumps(payload, ensure_ascii=False) for payload, _, _ in pages)
        self.assertNotIn("<at id=all>", rendered)
        for payload, _, _ in pages:
            self.assertLessEqual(len(json.dumps(payload, ensure_ascii=False).encode()), digest.MAX_CARD_BYTES)
        expected_fragments = (len(raw) + 999) // 1000
        self.assertEqual(expected_fragments, rendered.count("消息ID：1"))

    def test_empty_digest_does_not_send(self):
        self.assertEqual([], digest.render_pages("群", []))


class FakeMessage:
    def __init__(self, number, text, sender=7, at=1000):
        self.id = number
        self.raw_text = text
        self.sender_id = sender
        self.date = datetime.fromtimestamp(at, timezone.utc)
        self.media = None
        self.reply_to_msg_id = None

    async def get_sender(self):
        return SimpleNamespace(first_name=f"sender{self.sender_id}", last_name=None, username=None)


class FakeClient:
    def __init__(self, messages):
        self.messages = {m.id: m for m in messages}
        self.entity = SimpleNamespace(title="测试群", username=None)

    async def get_entity(self, ref):
        return self.entity

    async def get_messages(self, entity, ids=None, limit=None):
        if ids is not None:
            return [self.messages.get(number) for number in ids]
        return sorted(self.messages.values(), key=lambda x: x.id, reverse=True)[:limit]

    async def iter_messages(self, entity, min_id, reverse, limit):
        for message in sorted(self.messages.values(), key=lambda x: x.id):
            if message.id > min_id:
                if limit <= 0:
                    break
                yield message
                limit -= 1


class WorkflowTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "state.json"
        for patcher in [patch.object(poll, "STATE_PATH", self.path),
                        patch.object(poll.utils, "get_peer_id", return_value=-1)]:
            patcher.start()
            self.addCleanup(patcher.stop)
        self.state = {"-1": {"title": "测试群", "last_id": 1}}

    async def test_priority_now_normal_later_and_restart(self):
        client = FakeClient([FakeMessage(2, "日常讨论", sender=8),
                             FakeMessage(3, "AWS", sender=7, at=1010),
                             FakeMessage(4, "需要充值1000元，报价多少", sender=7, at=1020)])
        with patch.object(poll, "send_feishu") as send:
            await poll.process_chat(client, -1, self.state, now=1100)
            self.assertEqual(1, send.call_count)
            self.assertIn("优先提醒", json.dumps(send.call_args.args[0], ensure_ascii=False))
            self.assertIn("2条合并", json.dumps(send.call_args.args[0], ensure_ascii=False))
        disk = poll.load_state()
        self.assertEqual([2], [x["id"] for x in disk["-1"]["pending"]])
        self.assertNotIn("日常讨论", self.path.read_text())
        with patch.object(poll, "send_feishu") as send:
            await poll.process_chat(client, -1, disk, now=1500)
            send.assert_not_called()
            await poll.process_chat(client, -1, disk, now=1801)
            self.assertEqual(1, send.call_count)
            self.assertIn("日常讨论", json.dumps(send.call_args.args[0], ensure_ascii=False))
            self.assertEqual([], disk["-1"]["pending"])
            await poll.process_chat(client, -1, disk, now=2701)
            self.assertEqual(1, send.call_count)

    async def test_send_failure_keeps_unsent_but_acknowledges_sent_pages(self):
        messages = [FakeMessage(n, "需要 AWS 账号，求报价" + str(n), sender=n) for n in range(2, 26)]
        client = FakeClient(messages)
        sent_ids = []
        calls = 0

        def send(payload, fallback):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise TimeoutError("simulated timeout")
            import re
            sent_ids.extend(int(number) for number in re.findall(r"消息ID：(\d+)", fallback))

        with patch.object(poll, "send_feishu", side_effect=send):
            with self.assertRaises(TimeoutError):
                await poll.process_chat(client, -1, self.state, now=1100)
        disk = poll.load_state()
        pending_ids = [x["id"] for x in disk["-1"]["pending"]]
        self.assertTrue(sent_ids)
        self.assertFalse(set(sent_ids) & set(pending_ids))
        self.assertEqual(set(range(2, 26)), set(sent_ids + pending_ids))
        with patch.object(poll, "send_feishu") as send:
            await poll.process_chat(client, -1, disk, now=1200)
            self.assertGreater(send.call_count, 0)
        self.assertEqual([], disk["-1"]["pending"])

    async def test_deleted_pending_message_reported_at_digest(self):
        client = FakeClient([FakeMessage(2, "普通消息")])
        with patch.object(poll, "send_feishu") as send:
            await poll.process_chat(client, -1, self.state, now=1100)
            client.messages.clear()
            await poll.process_chat(client, -1, self.state, now=1801)
            self.assertIn("已删除", json.dumps(send.call_args.args[0], ensure_ascii=False))
        self.assertEqual([], self.state["-1"]["pending"])

    async def test_merge_across_polls_and_duplicate_persistence(self):
        text = "长期供应 AWS 新加坡服务器，欢迎咨询"
        client = FakeClient([FakeMessage(2, text, at=1000)])
        with patch.object(poll, "send_feishu") as send:
            await poll.process_chat(client, -1, self.state, now=1050)
            disk = poll.load_state()
            client.messages[3] = FakeMessage(3, "具体配置稍后发送", at=1070)
            await poll.process_chat(client, -1, disk, now=1150)
            client.messages[4] = FakeMessage(4, text, at=1400)
            await poll.process_chat(client, -1, disk, now=1801)
            self.assertEqual(1, send.call_count)
            card = json.dumps(send.call_args.args[0], ensure_ascii=False)
            self.assertIn("2条合并", card)
            self.assertIn("重复消息 1 条", card)
            self.assertEqual(4, disk["-1"]["last_id"])

    async def test_backlog_exceeds_fetch_limit_without_loss(self):
        client = FakeClient([FakeMessage(n, f"普通消息 {n}", sender=n) for n in range(2, 14)])
        with patch.object(poll, "BATCH_LIMIT", 5), patch.object(poll, "send_feishu") as send:
            await poll.process_chat(client, -1, self.state, now=1100)
            await poll.process_chat(client, -1, self.state, now=1400)
            await poll.process_chat(client, -1, self.state, now=1801)
            output = "\n".join(call.args[1] for call in send.call_args_list)
            for n in range(2, 14):
                self.assertIn(f"消息ID：{n}", output)
        self.assertEqual(13, self.state["-1"]["last_id"])
        self.assertEqual([], self.state["-1"]["pending"])

    async def test_initialization_does_not_replay_history(self):
        client = FakeClient([FakeMessage(10, "老消息")])
        with patch.object(poll, "send_feishu") as send:
            state = {}
            await poll.process_chat(client, -1, state, now=1100)
            send.assert_not_called()
        self.assertEqual(10, state["-1"]["last_id"])

    async def test_main_continues_other_chats_after_failure(self):
        client = AsyncMock()
        client.__aenter__.return_value = client
        poll.save_state(self.state)

        async def process(client, chat_ref, state):
            state[str(chat_ref)] = {"last_id": 20, "pending": [{"id": 20}]}
            if chat_ref == -1:
                raise TimeoutError("simulated failure")
            state[str(chat_ref)]["pending"] = []

        with patch.object(poll, "TelegramClient", return_value=client), \
             patch.object(poll, "StringSession"), \
             patch.object(poll, "required_env", return_value="123"), \
             patch.object(poll, "parse_chat_refs", return_value=[-1, -2]), \
             patch.object(poll, "process_chat", side_effect=process) as process_mock:
            with self.assertRaisesRegex(RuntimeError, "1 chat"):
                await poll.main()
        self.assertEqual(2, process_mock.call_count)
        disk = poll.load_state()
        self.assertEqual([{"id": 20}], disk["-1"]["pending"])
        self.assertEqual([], disk["-2"]["pending"])

    def test_missing_success_code_is_not_acknowledged(self):
        malformed = SimpleNamespace(raise_for_status=lambda: None, json=lambda: {})
        with patch.dict("os.environ", {"FEISHU_WEBHOOK": "https://example.test/hook", "FEISHU_SECRET": ""}), \
             patch.object(poll.time, "sleep"), \
             patch.object(poll.requests, "post", return_value=malformed):
            with self.assertRaisesRegex(RuntimeError, "Feishu webhook failed"):
                poll.send_feishu(poll.feishu_text_payload("test"), "test")

    def test_plain_text_fallback_does_not_truncate(self):
        fallback = "完整内容" * 3000
        rejected = SimpleNamespace(raise_for_status=lambda: None, json=lambda: {"code": 123})
        accepted = SimpleNamespace(raise_for_status=lambda: None, json=lambda: {"code": 0})
        with patch.dict("os.environ", {"FEISHU_WEBHOOK": "https://example.test/hook", "FEISHU_SECRET": ""}), \
             patch.object(poll.time, "sleep"), \
             patch.object(poll.requests, "post", side_effect=[rejected] + [accepted] * 10) as post:
            poll.send_feishu({"msg_type": "interactive"}, fallback)
        sent = "".join(call.kwargs["json"]["content"]["text"] for call in post.call_args_list[1:])
        self.assertEqual(fallback, sent)


if __name__ == "__main__":
    unittest.main()
