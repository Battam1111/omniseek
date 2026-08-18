from __future__ import annotations

import unittest
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from omniseek.core.curator.apply import _hosts_of_adapter


class CuratorAdapterHostTests(unittest.TestCase):
    def test_non_page_watch_adapter_never_calls_private_rows_loader(self):
        calls: list[str] = []

        class Adapter:
            name = "layoffs_tracker"

            @staticmethod
            def _rows():
                calls.append("called")
                return [{"url": "https://airtable.com/app-private"}]

        self.assertEqual(_hosts_of_adapter(Adapter()), set())
        self.assertEqual(calls, [])


if __name__ == "__main__":
    unittest.main()
