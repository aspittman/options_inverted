"""Universe expansion must retain earnings and correlation risk controls."""
import os
import subprocess
import sys
import unittest
from unittest.mock import patch

import config
import options_trader
import backtester
import main


class UniverseTests(unittest.TestCase):
    def test_expansion_preserves_original_symbols_and_order(self):
        self.assertEqual(config.EXPANDED_UNDERLYINGS[:40], config.ORIGINAL_UNDERLYINGS)
        self.assertEqual(len(config.EXPANDED_UNDERLYINGS), 72)
        self.assertEqual(len(set(config.EXPANDED_UNDERLYINGS)), 72)
        self.assertEqual(main.UNDERLYINGS, config.UNDERLYINGS)
        self.assertEqual(backtester.UNDERLYINGS, config.UNDERLYINGS)

    def test_all_candidates_have_exactly_one_correlation_group(self):
        for symbol in config.EXPANDED_UNDERLYINGS:
            with self.subTest(symbol=symbol):
                self.assertEqual(sum(symbol in group for group in config.CORRELATION_GROUPS.values()), 1)
        for first, second in [('XLF', 'BAC'), ('KRE', 'WFC'), ('XLE', 'KMI'),
                              ('XBI', 'PFE'), ('GDX', 'IAU'), ('EEM', 'FXI')]:
            self.assertEqual(config.correlation_group(first), config.correlation_group(second))

    def test_funds_skip_earnings_and_new_stocks_retain_guard(self):
        options_trader._earnings_cache.clear()
        with patch.object(options_trader.yf, 'Ticker') as ticker:
            for symbol in config.NON_CORPORATE_UNDERLYINGS:
                self.assertFalse(options_trader.has_earnings_soon(symbol))
            ticker.assert_not_called()
            ticker.side_effect = RuntimeError('earnings data unavailable')
            with patch.object(options_trader, 'record_event'):
                for symbol in set(config.AFFORDABLE_UNIVERSE_ADDITIONS)-config.NON_CORPORATE_UNDERLYINGS:
                    self.assertTrue(options_trader.has_earnings_soon(symbol))
        options_trader._earnings_cache.clear()

    def test_original_profile_and_invalid_profile(self):
        for profile, expected in [('original', '40'), ('expanded', '72')]:
            result = subprocess.run([sys.executable, '-c', 'import config; print(len(config.UNDERLYINGS))'],
                env=dict(os.environ, UNIVERSE_PROFILE=profile), capture_output=True, text=True)
            self.assertEqual(result.returncode, 0)
            self.assertEqual(result.stdout.strip(), expected)
        result = subprocess.run([sys.executable, '-c', 'import config'],
            env=dict(os.environ, UNIVERSE_PROFILE='typo'), capture_output=True, text=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("UNIVERSE_PROFILE must be", result.stderr)


if __name__ == '__main__':
    unittest.main()
