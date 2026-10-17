from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock

from trade_bot import EntryNotReady, Journal, entry_instruction, process_update, process_waiting


SAMPLE = (Path(__file__).parent / "fixtures" / "xau_buy.txt").read_text(encoding="utf-8")
CHAT = -1004418162453


def update(text, message=10, stamp=1000, topic=5, edited=False):
    return {"message": {"chat": {"id": CHAT}, "message_id": message, "date": stamp,
                        "text": text, "message_thread_id": topic, "is_edit": edited}}


class EntryTestCase(unittest.TestCase):
    def setUp(self):
        self.journal = Journal(":memory:")
        self.addCleanup(self.journal.db.close)
        self.broker = Mock()

    def process(self, message, **options):
        return process_update(message, {CHAT}, self.journal, self.broker, now=1001, **options)


class ControlTests(EntryTestCase):
    def test_stop_variations_and_explicit_resume_only(self):
        for text in ("don't enter anymore", "DONT ENTER", "Don’t enter", "do not enter",
                     "Cancel this setup", "cancel pending orders", "entry cancelled", "no more entries"):
            with self.subTest(text=text):
                self.assertEqual(entry_instruction(text), "pause")
        for text in ("resume entries", "You can enter now!", "resume trading"):
            self.assertEqual(entry_instruction(text), "resume")
        for text in ("we might resume entries later", "do not resume entries", "TP1 hit", "BUY XAUUSD"):
            self.assertIsNone(entry_instruction(text))
        self.assertEqual(entry_instruction("resume entries but don't enter gold"), "pause")

    def test_pause_blocks_signals_until_explicit_resume(self):
        self.assertEqual(self.process(update("Don't enter anymore", message=11)), "entries-paused")
        self.assertEqual(self.process(update(SAMPLE, message=12)), "blocked-paused")
        self.broker.prepare.assert_not_called()
        self.assertEqual(self.process(update("resume entries", message=13)), "entries-resumed")
        self.assertEqual(self.process(update(SAMPLE, message=14)), "executed")
        self.broker.execute.assert_called_once()

    def test_older_resume_cannot_override_newer_pause(self):
        self.process(update("don't enter", message=12))
        self.assertEqual(self.process(update("resume entries", message=11)), "outdated-control")
        self.assertTrue(self.journal.entries_paused(CHAT, 5))

    def test_edit_can_cancel_but_never_enter_or_resume(self):
        self.process(update(SAMPLE))
        self.assertEqual(self.process(update("don't enter", edited=True, stamp=1001)), "entries-paused")
        self.assertEqual(self.process(update("resume entries", edited=True, message=11, stamp=1001)), "edited-ignored")
        self.assertTrue(self.journal.entries_paused(CHAT, 5))
        self.assertEqual(self.process(update(SAMPLE, edited=True, message=12)), "edited-ignored")
        self.assertEqual(self.broker.execute.call_count, 1)

    def test_state_is_scoped_to_topic_and_chatter_is_recorded(self):
        self.process(update("don't enter", topic=6))
        self.assertFalse(self.journal.entries_paused(CHAT, 5))
        self.assertEqual(self.process(update("Price is moving slowly", message=11)), "unrecognized")
        self.assertEqual(self.journal.db.execute("SELECT text FROM observations WHERE message=11").fetchone()[0], "Price is moving slowly")
        self.broker.execute.assert_not_called()

    def test_pause_persists_across_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "journal.sqlite3"
            first = Journal(path)
            process_update(update("don't enter"), {CHAT}, first, now=1001)
            first.db.close()
            second = Journal(path)
            try:
                self.assertTrue(second.entries_paused(CHAT, 5))
                self.assertEqual(process_update(update(SAMPLE, message=11), {CHAT}, second, self.broker, now=1001), "blocked-paused")
            finally:
                second.db.close()
        self.broker.execute.assert_not_called()


class WaitingTests(EntryTestCase):
    def wait(self, message=10):
        self.broker.prepare.side_effect = EntryNotReady("Outside zone")
        self.assertEqual(self.process(update(SAMPLE, message=message)), "waiting")
        self.broker.execute.assert_not_called()

    def tick(self, **options):
        process_waiting(self.journal, self.broker, {CHAT}, topic_id=5, now=1002, **options)

    def test_zone_reached_executes_once(self):
        self.wait()
        self.tick()
        self.broker.execute.assert_not_called()
        self.broker.prepare.side_effect = None
        self.tick()
        self.tick()
        self.broker.execute.assert_called_once()
        self.assertEqual(self.journal.db.execute("SELECT status FROM waiting_entries").fetchone()[0], "executed")

    def test_dont_enter_cancels_waiting_setup_and_resume_does_not_revive_it(self):
        self.wait()
        self.assertEqual(self.process(update("dont enter anymore", message=11)), "entries-paused")
        self.broker.prepare.side_effect = None
        self.tick()
        self.process(update("resume entries", message=12))
        self.tick()
        self.broker.execute.assert_not_called()
        self.assertEqual(self.journal.db.execute("SELECT status FROM waiting_entries").fetchone()[0], "cancelled")

    def test_waiting_setup_expires_and_is_not_replayed_after_reconnect(self):
        self.wait()
        process_waiting(self.journal, self.broker, {CHAT}, topic_id=5, now=1900)
        self.assertEqual(self.journal.db.execute("SELECT status FROM waiting_entries").fetchone()[0], "expired")
        self.wait(message=11)
        self.journal.interrupt_waiting()
        self.broker.prepare.side_effect = None
        self.tick()
        self.broker.execute.assert_not_called()
        self.assertEqual(self.journal.db.execute("SELECT status FROM waiting_entries WHERE message=11").fetchone()[0], "interrupted")

    def test_new_setup_supersedes_previous_waiting_setup(self):
        self.wait()
        self.wait(message=11)
        rows = self.journal.db.execute("SELECT message, status FROM waiting_entries ORDER BY message").fetchall()
        self.assertEqual(rows, [(10, "superseded"), (11, "waiting")])

    def test_quote_paper_mode_never_submits(self):
        self.wait()
        self.broker.prepare.side_effect = None
        self.tick(execute_orders=False)
        self.broker.execute.assert_not_called()
        self.assertEqual(self.journal.db.execute("SELECT status FROM waiting_entries").fetchone()[0], "paper-ready")

    def test_waiting_submission_failure_is_not_retried(self):
        self.wait()
        self.broker.prepare.side_effect = None
        self.broker.execute.side_effect = RuntimeError("uncertain")
        with self.assertRaises(RuntimeError):
            self.tick()
        self.tick()
        self.broker.execute.assert_called_once()
        self.assertEqual(self.journal.db.execute("SELECT status FROM waiting_entries").fetchone()[0], "submitting")
