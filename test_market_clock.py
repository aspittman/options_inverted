"""Market-clock failures must wait safely without exiting or assuming open."""
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from alpaca.common.exceptions import APIError
from requests.exceptions import ConnectionError, Timeout

from strategy import wait_for_market_open


class MarketClockTests(unittest.TestCase):
    @patch('strategy.bot_log')
    @patch('time.sleep')
    def test_api_500_retries_then_waits_for_confirmed_open(self, sleep, log):
        client = Mock()
        client.get_clock.side_effect = [
            APIError('{"message":"Internal Server Error"}'),
            SimpleNamespace(is_open=False),
            SimpleNamespace(is_open=True),
        ]
        wait_for_market_open(client)
        self.assertEqual(client.get_clock.call_count, 3)
        self.assertEqual([call.args for call in sleep.call_args_list], [(60,), (60,)])
        self.assertIn('Retrying in 60 seconds', log.call_args_list[0].args[0])
        self.assertEqual(log.call_args_list[-1].args[0], 'Market is open.')

    @patch('strategy.bot_log')
    @patch('time.sleep')
    def test_network_failures_still_retry(self, sleep, log):
        client = Mock()
        client.get_clock.side_effect = [ConnectionError('offline'), Timeout('timeout'),
                                       SimpleNamespace(is_open=True)]
        wait_for_market_open(client)
        self.assertEqual(client.get_clock.call_count, 3)
        self.assertEqual(sleep.call_count, 2)

    @patch('strategy.bot_log')
    @patch('time.sleep', side_effect=KeyboardInterrupt)
    def test_persistent_failure_remains_interruptible(self, sleep, log):
        client = Mock()
        client.get_clock.side_effect = APIError('{"message":"Internal Server Error"}')
        with self.assertRaises(KeyboardInterrupt):
            wait_for_market_open(client)
        self.assertEqual(client.get_clock.call_count, 1)
        self.assertFalse(any(c.args[0] == 'Market is open.' for c in log.call_args_list))

    @patch('time.sleep')
    def test_unexpected_programming_error_is_not_hidden(self, sleep):
        client = Mock()
        client.get_clock.side_effect = ValueError('unexpected clock bug')
        with self.assertRaises(ValueError):
            wait_for_market_open(client)
        sleep.assert_not_called()


if __name__ == '__main__':
    unittest.main()
