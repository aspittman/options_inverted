import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace as NS
from unittest.mock import patch
import analytics
import config
import options_trader


class LongTrailingTests(unittest.TestCase):
    def test_default_enabled(self):
        self.assertEqual(config.OPTION_TRAILING_STOP_PERCENT, .20)

    def test_persisted_peak_survives_partial_exit_and_resets_on_reentry(self):
        rows=[]
        def add(event, side='', price=0, qty=0):
            rows.append(dict(event=event, strategy='oasis', option_symbol='ABC',
                             order_side=side, price=price, qty=qty))
        add('ORDER_FILL','buy',4,2)
        add('OPTION_TRAIL_SNAPSHOT',price=6)
        add('ORDER_FILL','sell',5,1)
        with patch.object(analytics,'read_events',return_value=rows):
            self.assertEqual(analytics.get_option_high_water_marks(),{('oasis','ABC'):6})
            add('ORDER_FILL','sell',5,1)
            self.assertEqual(analytics.get_option_high_water_marks(),{})
            add('ORDER_FILL','buy',3,1)
            self.assertEqual(analytics.get_option_high_water_marks(),{('oasis','ABC'):3})

    def test_only_oasis_exits_on_premium_reversal_after_reloading_ledger(self):
        for variant in ('regular','oasis'):
            with self.subTest(variant=variant):
                now=datetime.now(timezone.utc);expiry=now.date()+timedelta(days=75)
                kind=config.OPTION_TYPE[0].upper()
                symbol=f'ABC{expiry:%y%m%d}{kind}00100000'
                rows=[dict(event='ORDER_FILL',strategy=variant,option_symbol=symbol,
                           underlying='ABC',order_side='buy',qty=1,price=4)]
                lot=dict(qty=1,cost=4,underlying_cost=100,opened_at=now.isoformat())
                position=NS(symbol=symbol,current_price=6)
                clock=NS(is_open=True,timestamp=now,next_close=now+timedelta(hours=2))
                positions_name='get_options_direct_positions' if config.OPTION_TYPE=='call' else 'get_options_inverted_positions'
                water_name='get_underlying_high_water_marks' if config.OPTION_TYPE=='call' else 'get_underlying_low_water_marks'
                def record(event,**kwargs):
                    rows.append(dict(event=event,**kwargs))
                with patch.object(analytics,'read_events',side_effect=lambda:list(rows)), \
                     patch.object(options_trader,positions_name,return_value=[position]), \
                     patch.object(options_trader,'get_strategy_open_lots',return_value={(variant,'ABC',symbol):lot}), \
                     patch.object(options_trader,water_name,return_value={}), \
                     patch.object(options_trader,'get_underlying_price',return_value=100), \
                     patch.object(options_trader.trading_client,'get_clock',return_value=clock), \
                     patch.object(options_trader,'record_event',side_effect=record), \
                     patch.object(options_trader,'close_strategy_lot') as close:
                    options_trader.manage_underlying_exits(['ABC'],lambda *args:(False,''),.03,.08)
                    close.assert_not_called()
                    self.assertEqual(any(r['event']=='OPTION_TRAIL_SNAPSHOT' for r in rows), variant == 'oasis')
                    # No process-local cache: a new invocation must rebuild the saved $6 peak.
                    position.current_price=4.8
                    options_trader.manage_underlying_exits(['ABC'],lambda *args:(False,''),.03,.08)
                    if variant == 'oasis':
                        self.assertIn('option_trailing_stop',close.call_args.args[-1])
                    else:
                        close.assert_not_called()

    def test_real_csv_retains_oasis_fills_and_trailing_snapshots(self):
        import tempfile
        from pathlib import Path
        kind=config.OPTION_TYPE[0].upper()
        symbol=f'ABC261218{kind}00100000'
        with tempfile.TemporaryDirectory() as tmp, patch.object(analytics,'ANALYTICS_FILE',str(Path(tmp)/'trades.csv')):
            analytics.record_event('ORDER_FILL',strategy='oasis',underlying='ABC',
                                   option_symbol=symbol,qty=1,price=4,order_side='buy')
            analytics.record_event('OPTION_TRAIL_SNAPSHOT',strategy='oasis',underlying='ABC',
                                   option_symbol=symbol,qty=1,price=6)
            self.assertEqual(len(analytics.read_events()),2)
            self.assertEqual(analytics.get_option_high_water_marks(),{('oasis',symbol):6})
            self.assertIn(('oasis','ABC',symbol),analytics.get_strategy_open_lots())
            analytics.record_event('ORDER_FILL',strategy='oasis',underlying='ABC',
                                   option_symbol=symbol,qty=1,price=5,order_side='sell')
            self.assertEqual(analytics.get_option_high_water_marks(),{})
            analytics.record_event('ORDER_FILL',strategy='oasis',underlying='ABC',
                                   option_symbol=symbol,qty=1,price=3,order_side='buy')
            self.assertEqual(analytics.get_option_high_water_marks(),{('oasis',symbol):3})
