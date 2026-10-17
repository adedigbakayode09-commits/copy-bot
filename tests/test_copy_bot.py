import tempfile
from pathlib import Path
import unittest
from unittest.mock import Mock

from copy_bot import TelegramError, copy_update, save_offset, settings


class CopyBotTests(unittest.TestCase):
    def test_only_selected_groups_are_copied(self):
        api = Mock()
        for chat_id in (-1001, -1002, -1003):
            copy_update(api, {"message": {"chat": {"id": chat_id}, "message_id": 7}}, {-1001}, -1009)
        api.call.assert_called_once_with("copyMessage", chat_id=-1009, from_chat_id=-1001, message_id=7)

    def test_channel_posts_are_copied(self):
        api = Mock()
        self.assertTrue(copy_update(api, {"channel_post": {"chat": {"id": -1001}, "message_id": 8}}, {-1001}, -1009))

    def test_edited_and_unrelated_updates_are_ignored(self):
        api = Mock()
        for update in ({}, {"edited_message": {"chat": {"id": -1001}}}, {"message": {"chat": {"id": -1002}}}):
            self.assertFalse(copy_update(api, update, {-1001}, -1009))
        api.call.assert_not_called()

    def test_delivery_failure_propagates(self):
        api = Mock()
        api.call.side_effect = TelegramError("failed")
        with self.assertRaises(TelegramError):
            copy_update(api, {"message": {"chat": {"id": -1001}, "message_id": 8}}, {-1001}, -1009)

    def test_configuration_fails_closed(self):
        base = {"TELEGRAM_BOT_TOKEN": "test", "SOURCE_CHAT_IDS": "-1001,-1002", "DESTINATION_CHAT_ID": "-1009"}
        self.assertEqual(settings(base), ("test", {-1001, -1002}, -1009))
        for override in ({"TELEGRAM_BOT_TOKEN": ""}, {"SOURCE_CHAT_IDS": ""}, {"SOURCE_CHAT_IDS": "abc"}, {"DESTINATION_CHAT_ID": "-1001"}):
            with self.assertRaises(ValueError):
                settings(base | override)

    def test_offset_survives_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state" / "offset"
            save_offset(path, 42)
            self.assertEqual(path.read_text(), "42")
            save_offset(path, 43)
            self.assertEqual(path.read_text(), "43")


if __name__ == "__main__":
    unittest.main()
