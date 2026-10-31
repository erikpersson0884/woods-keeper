import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import storage


class StorageTestCase(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        storage.STATUS_FILE = str(Path(self.temp_dir.name) / "nested" / "status.json")
        storage.SCHEDULE_FILE = str(Path(self.temp_dir.name) / "nested" / "schedule.json")

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_status_round_trip(self):
        value = {"status": "Success", "logs": ["done"]}
        storage.save_status(value)

        self.assertEqual(storage.load_status(), value)

    def test_invalid_status_file_uses_defaults(self):
        Path(storage.STATUS_FILE).parent.mkdir()
        Path(storage.STATUS_FILE).write_text("{invalid")

        self.assertEqual(storage.load_status()["status"], "Idle")

    def test_valid_schedule_normalizes_values(self):
        storage.save_schedule({
            "enabled": 1,
            "interval": "15m",
            "excluded_stack_ids": [7, "8"],
        })

        self.assertEqual(
            storage.load_schedule(),
            {"enabled": True, "interval": "15m", "excluded_stack_ids": ["7", "8"]},
        )

    def test_invalid_schedule_uses_defaults(self):
        Path(storage.SCHEDULE_FILE).parent.mkdir()
        Path(storage.SCHEDULE_FILE).write_text(json.dumps({"interval": "never"}))

        self.assertEqual(storage.load_schedule(), storage.DEFAULT_SCHEDULE)

    def test_unreadable_files_use_defaults(self):
        with patch("storage.os.path.exists", return_value=True), patch(
            "builtins.open", side_effect=OSError("unreadable")
        ):
            self.assertEqual(storage.load_status()["status"], "Idle")
            self.assertEqual(storage.load_schedule(), storage.DEFAULT_SCHEDULE)


if __name__ == "__main__":
    unittest.main()
