from __future__ import annotations

import unittest

from openclaw_iphone.runner import shell_quote


class RunnerTests(unittest.TestCase):
    def test_shell_quote_leaves_safe_values_unquoted(self) -> None:
        self.assertEqual(shell_quote("com.burbn.instagram"), "com.burbn.instagram")

    def test_shell_quote_quotes_spaces(self) -> None:
        self.assertEqual(shell_quote("Pearl's iPhone"), "'Pearl'\"'\"'s iPhone'")


if __name__ == "__main__":
    unittest.main()
