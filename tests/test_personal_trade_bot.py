import asyncio
from contextlib import suppress
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

from personal_trade_bot import consume, main, normalize_event, personal_config, read_topic, resolve_sources
from trade_bot import ExecutionPolicy, Journal


SAMPLE = (Path(__file__).parent / "fixtures" / "xau_buy.txt").read_text(encoding="utf-8")


def event(chat=-1001, outgoing=False, service=None, group=True, channel=True):
    return SimpleNamespace(chat_id=chat, raw_text=SAMPLE, is_group=group, is_channel=channel,
                           message=SimpleNamespace(id=7, out=outgoing, action=service,
                                                   date=datetime.fromtimestamp(1000, timezone.utc)))


class PersonalTests(unittest.TestCase):
    def test_topic_configuration_never_silently_defaults_to_every_topic(self):
        self.assertEqual(read_topic({"SOURCE_TOPIC_ID": "5"}), 5)
        self.assertIsNone(read_topic({"SOURCE_TOPIC_ID": "all"}))
        for value in ("", "0", "-5", "bad", "5,6"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                read_topic({"SOURCE_TOPIC_ID": value})

    def test_topic_accepts_direct_posts_and_replies_in_selected_topic(self):
        for header in (SimpleNamespace(forum_topic=True, reply_to_top_id=None, reply_to_msg_id=5),
                       SimpleNamespace(forum_topic=True, reply_to_top_id=5, reply_to_msg_id=4393)):
            message = event(chat=-1004418162453)
            message.message.reply_to = header
            self.assertIsNotNone(normalize_event(message, {-1004418162453}, topic_id=5))

    def test_other_topics_general_chat_and_ordinary_replies_are_excluded(self):
        for header in (None, SimpleNamespace(forum_topic=True, reply_to_top_id=6, reply_to_msg_id=5),
                       SimpleNamespace(forum_topic=True, reply_to_top_id=None, reply_to_msg_id=6),
                       SimpleNamespace(forum_topic=False, reply_to_top_id=None, reply_to_msg_id=5)):
            message = event(chat=-1004418162453)
            message.message.reply_to = header
            self.assertIsNone(normalize_event(message, {-1004418162453}, topic_id=5))
        message = event(chat=-1002)
        message.message.reply_to = SimpleNamespace(forum_topic=True, reply_to_top_id=5, reply_to_msg_id=4393)
        self.assertIsNone(normalize_event(message, {-1004418162453}, topic_id=5))

    def test_login_failure_reports_error_type_without_private_error_message(self):
        with patch("sys.argv", ["personal_trade_bot.py", "--login"]), patch("personal_trade_bot.run", new=AsyncMock(side_effect=RuntimeError("private credential data"))):
            with self.assertRaises(SystemExit) as result:
                main()
        self.assertIn("RuntimeError", str(result.exception))
        self.assertNotIn("private credential data", str(result.exception))

    def test_selected_group_name_resolves_to_only_its_numeric_id(self):
        dialogs = [SimpleNamespace(id=-1001, name="TheGoldenEdge V2", is_group=True, is_channel=True),
                   SimpleNamespace(id=-1002, name="Other signals", is_group=True, is_channel=False)]
        selected = resolve_sources({"SOURCE_CHAT_NAME": "TheGoldenEdge V2"}, dialogs)
        self.assertEqual(selected, {-1001})
        self.assertIsNone(normalize_event(event(chat=-1002), selected))

    def test_name_matching_does_not_guess_or_choose_between_duplicates(self):
        match = SimpleNamespace(id=-1001, name="TheGoldenEdge V2", is_group=True, is_channel=True)
        duplicate = SimpleNamespace(id=-1002, name="TheGoldenEdge V2", is_group=True, is_channel=True)
        private = SimpleNamespace(id=123, name="TheGoldenEdge V2", is_group=False, is_channel=False)
        for config, dialogs in (({}, [match]), ({"SOURCE_CHAT_NAME": "thegoldenedge v2"}, [match]),
                                ({"SOURCE_CHAT_NAME": "TheGoldenEdge V2"}, [private]),
                                ({"SOURCE_CHAT_NAME": "TheGoldenEdge V2"}, [match, duplicate])):
            with self.subTest(config=config), self.assertRaises(ValueError):
                resolve_sources(config, dialogs)
        self.assertEqual(resolve_sources({"SOURCE_CHAT_IDS": "-1002", "SOURCE_CHAT_NAME": match.name}, [match, duplicate]), {-1002})
        with self.assertRaises(ValueError):
            resolve_sources({"SOURCE_CHAT_IDS": "-1009", "SOURCE_CHAT_NAME": match.name}, [match])

    def test_personal_api_credentials_are_required_without_bot_token(self):
        config = {"TELEGRAM_API_ID": "123", "TELEGRAM_API_HASH": "local-placeholder"}
        api_id, api_hash, session = personal_config(config)
        self.assertEqual(api_id, 123)
        self.assertEqual(api_hash, "local-placeholder")
        self.assertTrue(session.name.endswith(".session"))
        for override in ({"TELEGRAM_API_ID": ""}, {"TELEGRAM_API_ID": "-1"},
                         {"TELEGRAM_API_HASH": ""}, {"TELEGRAM_SESSION_PATH": "bad.sqlite"}):
            with self.subTest(override=override), self.assertRaises(ValueError):
                personal_config(config | override)

    def test_only_allowed_incoming_group_messages_are_normalized(self):
        update = normalize_event(event(), {-1001})
        self.assertEqual(update["message"]["chat"]["id"], -1001)
        self.assertEqual(update["message"]["text"], SAMPLE)
        self.assertEqual(update["message"]["date"], 1000)
        for ignored in (event(chat=-1002), event(outgoing=True), event(service="join"),
                        event(group=False, channel=False)):
            self.assertIsNone(normalize_event(ignored, {-1001}))


class PersonalWorkerTests(unittest.IsolatedAsyncioTestCase):
    async def test_incoming_signal_reaches_paper_journal_without_submitting(self):
        journal = Journal(":memory:")
        self.addCleanup(journal.db.close)
        queue = asyncio.Queue()
        await queue.put(normalize_event(event(), {-1001}))
        with patch("trade_bot.time.time", return_value=1001), self.assertLogs(level="INFO") as logs:
            worker = asyncio.create_task(consume(queue, {-1001}, journal, None, ExecutionPolicy()))
            try:
                await asyncio.wait_for(queue.join(), timeout=2)
            finally:
                worker.cancel()
                with suppress(asyncio.CancelledError):
                    await worker
        self.assertEqual(journal.db.execute("SELECT status FROM messages").fetchone()[0], "paper")
        self.assertTrue(any("0.05" in line and "4182" in line for line in logs.output))

    async def test_submission_failure_stops_worker_before_next_signal(self):
        journal = Journal(":memory:")
        self.addCleanup(journal.db.close)
        queue = asyncio.Queue()
        await queue.put(normalize_event(event(), {-1001}))
        second = event()
        second.message.id = 8
        await queue.put(normalize_event(second, {-1001}))
        broker = Mock()
        broker.execute.side_effect = RuntimeError("uncertain order")
        with patch("trade_bot.time.time", return_value=1001), self.assertRaises(RuntimeError):
            await consume(queue, {-1001}, journal, broker, ExecutionPolicy())
        self.assertEqual(queue.qsize(), 1)
        broker.execute.assert_called_once()
        self.assertEqual(journal.db.execute("SELECT status FROM messages").fetchone()[0], "submitting")
