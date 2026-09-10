"""Offline capital, shared-account ownership and research regression tests."""
import csv
import json
import tempfile
import unittest
from contextlib import ExitStack
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import patch, MagicMock

import analytics
import backtester
import config
import options_trader as trader
import research
from capital import capital_rejection, limit_price_at_mid
from alpaca_option_backtest import AlpacaOptionRepricer
from alpaca.trading.enums import PositionIntent

SYMBOL = f"SPY{date.today()+timedelta(days=75):%y%m%d}P00500000"

def fill(side, price, timestamp, qty=1, symbol=SYMBOL, **extra):
    return dict(event='ORDER_FILL', strategy='regular', bot_strategy='long_put',
                underlying='SPY', option_symbol=symbol, order_side=side,
                qty=qty, price=price, timestamp=timestamp, **extra)


def candidate(symbol, price, start='2026-01-01', end='2026-01-03', variant='regular', exit_price=None):
    return dict(symbol=symbol, option_symbol=f'{symbol}260417P00100000', strategy=variant,
                contracts=1, entry_date=start, exit_date=end,
                estimated_option_entry_price=price,
                estimated_option_exit_price=price if exit_price is None else exit_price,
                pnl_dollars=((price if exit_price is None else exit_price)-price)*100)


class LiveCapitalTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack(); self.addCleanup(self.stack.close)
        def mock(name, **kw): return self.stack.enter_context(patch('options_trader.'+name, **kw))
        self.submit = mock('trading_client.submit_order', return_value=NS(id='owned-order', status='new'))
        self.broker_positions=mock('trading_client.get_all_positions', return_value=[])
        mock('trading_client.get_orders', return_value=[])
        self.lots=mock('get_strategy_open_lots', return_value={})
        self.pending=mock('get_submitted_orders', return_value={})
        self.state=mock('capital_snapshot', return_value=dict(available=25000, employed=0, unresolved=0))
        self.snapshot=NS(latest_quote=NS(bid_price=3.39, ask_price=3.41), greeks=NS(delta=-0.6))
        mock('get_option_snapshots', return_value={SYMBOL:self.snapshot})
        mock('get_underlying_price', return_value=495)
        mock('ENABLE_NEW_ENTRIES', new=True)
        self.event=mock('record_event')
        self.reject=mock('record_rejection')

    def buy(self, **kw):
        return trader.buy_option_contract(SYMBOL, underlying='SPY', **kw)

    def test_340_dollars_buys_one_put_with_identified_order(self):
        self.assertTrue(self.buy())
        order=self.submit.call_args.args[0]
        self.assertEqual(order.qty,1)
        self.assertEqual(order.limit_price,3.4)
        self.assertEqual(order.position_intent,PositionIntent.BUY_TO_OPEN)
        self.assertTrue(order.client_order_id.startswith('long_put_SPY_'))
        self.assertLessEqual(len(order.client_order_id),48)

    def test_625_rejected_even_if_caller_has_no_limit(self):
        self.snapshot.latest_quote=NS(bid_price=6.24,ask_price=6.26)
        self.assertFalse(self.buy(max_entry_premium=None))
        self.submit.assert_not_called()
        self.assertEqual(self.reject.call_args.args[0],'PREMIUM_OVER_LIMIT')

    def test_legacy_or_caller_limit_cannot_raise_500_ceiling(self):
        self.snapshot.latest_quote=NS(bid_price=6.24,ask_price=6.26)
        self.assertFalse(self.buy(max_entry_premium=10000))
        self.submit.assert_not_called()

    def test_rounded_actual_order_must_fit_budget(self):
        self.snapshot.latest_quote=NS(bid_price=5.,ask_price=5.01)
        self.assertEqual(limit_price_at_mid(5,5.01),5.01)
        self.assertFalse(self.buy())
        self.assertEqual(self.reject.call_args.args[0],'PREMIUM_OVER_LIMIT')

    def test_exact_500_allowed(self):
        self.snapshot.latest_quote=NS(bid_price=4.99,ask_price=5.01)
        self.assertTrue(self.buy())

    def test_pending_exposure_and_virtual_cash_are_enforced(self):
        for available,employed in [(25000,800),(100,0)]:
            self.state.return_value=dict(available=available, employed=employed, unresolved=0)
            self.assertFalse(self.buy())
            self.assertEqual(self.reject.call_args.args[0],'MAX_STRATEGY_EXPOSURE_REACHED')
        self.submit.assert_not_called()

    def test_contract_count_and_entry_pause_cannot_be_bypassed(self):
        self.assertFalse(self.buy(qty=2))
        self.assertEqual(self.reject.call_args.args[0],'MAX_CONTRACTS_REACHED')
        with patch.object(trader,'ENABLE_NEW_ENTRIES',False): self.assertFalse(self.buy())
        self.submit.assert_not_called()

    def test_shared_account_short_put_is_not_bought_to_close(self):
        self.broker_positions.return_value=[NS(symbol=SYMBOL,qty=-1)]
        self.assertFalse(self.buy())
        self.assertEqual(self.reject.call_args.args[0],'DUPLICATE_POSITION')
        self.submit.assert_not_called()

    def test_entry_rechecks_spread_and_delta(self):
        self.snapshot.latest_quote=NS(bid_price=3,ask_price=4)
        self.assertFalse(self.buy())
        self.assertEqual(self.reject.call_args.args[0],'SPREAD_TOO_WIDE')
        self.snapshot.latest_quote=NS(bid_price=3.39,ask_price=3.41)
        self.snapshot.greeks.delta=0.6
        self.assertFalse(self.buy())
        self.submit.assert_not_called()

    def test_submit_boundary_enforces_position_and_group_limits(self):
        self.pending.return_value={'1':dict(strategy='max_100',underlying='QQQ',option_symbol='QQQ261218P00500000',order_side='buy')}
        self.assertFalse(self.buy())
        self.assertEqual(self.reject.call_args.args[0],'MAX_STRATEGY_EXPOSURE_REACHED')
        self.submit.assert_not_called()


class LedgerResearchTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.path=Path(self.tmp.name)/'events.csv'
        self.patch=patch.object(analytics,'ANALYTICS_FILE',str(self.path));self.patch.start();self.addCleanup(self.patch.stop)

    def record(self, event, **kw):
        analytics.record_event(event, strategy='regular', underlying='SPY', option_symbol=SYMBOL, **kw)

    def test_cancel_request_preserves_reservation_until_terminal(self):
        self.record('ORDER_SUBMITTED',qty=1,price=4,order_id='id',order_side='buy')
        self.record('CANCEL_REQUESTED',order_id='id')
        self.assertEqual(research.capital_snapshot()['pending_premium'],400)
        self.record('ORDER_TERMINAL',order_id='id',order_status='cancel_requested')
        self.assertIn('id',analytics.get_submitted_orders())
        self.record('ORDER_TERMINAL',order_id='id',order_status='canceled')
        self.assertEqual(research.capital_snapshot()['pending_premium'],0)

    def test_partial_fill_reserves_only_remaining_qty_and_survives_restart(self):
        self.record('ORDER_SUBMITTED',qty=1,price=4,order_id='id',order_side='buy')
        self.record('ORDER_FILL',qty=.5,price=3.9,order_id='id',order_side='buy')
        self.assertEqual(analytics.get_submitted_orders()['id']['remaining_qty'],.5)
        state=research.capital_snapshot()
        self.assertEqual(state['pending_premium'],200)
        self.assertEqual(state['employed'],395)
        self.record('ORDER_FILL',qty=.5,price=3.9,order_id='id',order_side='buy')
        self.assertEqual(analytics.get_submitted_orders(),{})
        self.assertEqual(research.capital_snapshot()['employed'],390)

    def test_shared_variants_have_one_25000_allocation(self):
        self.record('ORDER_FILL',qty=1,price=3,order_side='buy')
        analytics.record_event('ORDER_FILL',strategy='max_100',underlying='BAC',
            option_symbol='BAC261218P00040000',qty=1,price=2,order_side='buy')
        report=research.portfolio_report(analytics.read_events())
        self.assertEqual(report['starting_virtual_capital'],25000)
        self.assertEqual(report['current_capital_employed'],500)
        self.assertEqual(report['virtual_cash'],24500)

    def test_missing_position_is_unresolved_not_fabricated_win_or_loss(self):
        self.record('ORDER_FILL',qty=1,price=3,order_side='buy')
        self.record('POSITION_MISSING')
        state=research.capital_snapshot()
        self.assertEqual(state['unresolved'],1)
        self.assertEqual(state['employed'],300)
        report=research.portfolio_report(analytics.read_events())
        self.assertEqual(report['trade_count'],0)
        self.assertEqual(report['contracts_expired_worthless'],0)

    def test_foreign_rows_and_calls_excluded_and_old_local_puts_retained(self):
        self.record('ORDER_FILL',qty=1,price=3,order_side='buy')
        with self.path.open('a',newline='') as stream:
            writer=csv.DictWriter(stream,fieldnames=analytics.FIELDNAMES)
            for row in [fill('buy',9,'2026-01-01',bot_strategy_override='cash_secured_put'),
                        fill('buy',9,'2026-01-01',symbol=SYMBOL[:9]+'C'+SYMBOL[10:])]:
                row['bot_strategy']=row.pop('bot_strategy_override',row['bot_strategy'])
                writer.writerow(row)
        self.assertEqual(len(analytics.read_events()),1)
        self.assertEqual(research.capital_snapshot()['employed'],300)

    def test_rejection_has_metadata_and_does_not_count_as_fill(self):
        rejected=Path(self.tmp.name)/'rejected.csv'
        with patch.object(research,'REJECTED_TRADES_FILE',str(rejected)):
            with research.qualified_opportunity('regular','SPY','2026-09-10'):
                research.update_opportunity(contract_symbol=SYMBOL,bid=6.24,ask=6.26,virtual_capital_available=25000)
                self.record('SKIP',reason='PREMIUM_OVER_LIMIT')
        with rejected.open(newline='') as stream: row=list(csv.DictReader(stream))[0]
        self.assertEqual(row['strategy'],'long_put')
        self.assertEqual(row['rejection_reason'],'PREMIUM_OVER_LIMIT')
        self.assertEqual(float(row['required_capital']),625)
        self.assertEqual(float(row['strike']),500)
        self.assertEqual(row['signal_score'],'')
        self.assertEqual(research.portfolio_report(analytics.read_events())['trade_count'],0)

    def test_metrics_use_allocation_and_premium_denominators(self):
        events=[fill('buy',4,'2026-01-01T12:00:00'),fill('sell',5,'2026-01-03T12:00:00'),
                fill('buy',2,'2026-01-04T12:00:00'),fill('sell',1,'2026-01-08T12:00:00')]
        report=research.portfolio_report(events)
        self.assertEqual(report['premium_paid'],600)
        self.assertEqual(report['premium_lost'],100)
        self.assertEqual(report['trade_count'],2)
        self.assertEqual(report['average_hold_days'],3)
        self.assertEqual(report['win_rate_percent'],50)
        self.assertEqual(report['profit_factor'],1)
        self.assertEqual(report['max_drawdown'],100)
        self.assertEqual(report['maximum_capital_employed'],400)
        self.assertEqual(report['average_option_premium'],300)
        events=events[:2]
        report=research.portfolio_report(events)
        self.assertAlmostEqual(report['total_return_percent'],.4)
        self.assertEqual(report['return_on_capital_employed_percent'],25)
        self.assertEqual(report['ending_virtual_capital'],25100)

    def test_partial_exits_count_one_completed_round_trip(self):
        events=[fill('buy',4,'2026-01-01T12:00:00'),fill('sell',5,'2026-01-03T12:00:00',qty=.5),
                fill('sell',3,'2026-01-04T12:00:00',qty=.5)]
        self.assertEqual(research.portfolio_report(events)['trade_count'],1)


class OwnershipTests(unittest.TestCase):
    def test_never_adopts_legacy_or_other_bot_inventory(self):
        with patch.object(trader.trading_client,'get_all_positions') as broker:
            trader.bootstrap_legacy_positions()
        broker.assert_not_called()
        lots={('regular','SPY',SYMBOL):dict(qty=1,cost=3)}
        with patch.object(trader,'get_strategy_open_lots',return_value=lots), patch.object(trader.trading_client,'get_all_positions',return_value=[
            NS(symbol=SYMBOL,qty=3,current_price=4),NS(symbol='BAC261218P00040000',qty=-1),NS(symbol='AAPL',qty=100)]):
            owned=trader.get_options_inverted_positions()
        self.assertEqual(len(owned),1)
        self.assertEqual(owned[0].qty,1)
        self.assertEqual(owned[0].unrealized_pl,100)

    def test_close_requires_ledger_quantity_and_positive_broker_quantity(self):
        with patch.object(trader,'get_strategy_open_lots',return_value={}), patch.object(trader.trading_client,'submit_order') as submit:
            self.assertFalse(trader.close_strategy_lot('regular','SPY',SYMBOL,1,'test'))
            submit.assert_not_called()
        with patch.object(trader,'get_strategy_open_lots',return_value={('regular','SPY',SYMBOL):dict(qty=1)}), \
             patch.object(trader.trading_client,'get_all_positions',return_value=[NS(symbol=SYMBOL,qty=-1)]), \
             patch.object(trader,'get_submitted_orders',return_value={}), \
             patch.object(trader.trading_client,'submit_order') as submit:
            self.assertFalse(trader.close_strategy_lot('regular','SPY',SYMBOL,1,'test'))
            submit.assert_not_called()

    def test_foreign_order_is_never_cancelled_or_reconciled(self):
        submitted={'id':dict(option_symbol=SYMBOL,order_side='buy',qty=1,timestamp='2020-01-01')}
        with patch.object(trader,'get_submitted_orders',return_value=submitted), \
             patch.object(trader.trading_client,'get_order_by_id',return_value=NS(symbol=SYMBOL,client_order_id='cash_secured_put_SPY_x')), \
             patch.object(trader.trading_client,'cancel_order_by_id') as cancel, patch.object(trader,'record_event') as record:
            trader.reconcile_order_fills()
        cancel.assert_not_called();record.assert_not_called()


class HistoricalCapitalTests(unittest.TestCase):
    def test_premium_comparison_uses_same_contract_candidates(self):
        candidates=[candidate('A',3.4),candidate('B',6.25)]
        rejected=[]
        allowed=backtester.apply_portfolio_constraints(candidates,max_entry_premium=500,rejected=rejected)
        self.assertEqual([t['symbol'] for t in allowed],['A'])
        self.assertEqual(rejected[0]['rejection_reason'],'PREMIUM_OVER_LIMIT')
        self.assertEqual([t['symbol'] for t in backtester.apply_portfolio_constraints(candidates,max_entry_premium=750)],['A','B'])
        self.assertEqual(backtester.apply_portfolio_constraints(candidates,max_entry_premium=250),[])

    def test_shared_variants_exposure_count_and_cash(self):
        candidates=[candidate('A',4),candidate('B',4,variant='max_100'),candidate('C',4)]
        rejected=[]
        self.assertEqual(len(backtester.apply_portfolio_constraints(candidates,rejected=rejected)),2)
        self.assertEqual(rejected[0]['rejection_reason'],'MAX_STRATEGY_EXPOSURE_REACHED')
        self.assertEqual(len(backtester.apply_portfolio_constraints(candidates,starting_cash=500)),1)

    def test_config_output_balances_rejection_counts(self):
        with tempfile.TemporaryDirectory() as tmp:
            prefix=str(Path(tmp)/'500')
            report=backtester.report_configuration([candidate('A',3.4),candidate('B',6.25)],500,25000,prefix)
            self.assertEqual(report['qualified_signals'],report['executed_trades']+sum(report['rejected'].values()))
            self.assertEqual(report['maximum_capital_employed'],340)
            self.assertEqual(report['ending_virtual_capital'],25000)
            self.assertTrue(Path(prefix+'_rejected.csv').exists())
            json.loads(Path(prefix+'_summary.json').read_text())

    def test_historical_repricer_does_not_fall_back_to_cheaper_strike(self):
        repricer=AlpacaOptionRepricer()
        expensive=NS(symbol=SYMBOL,strike_price=101.55,expiration_date=date(2026,3,17))
        cheap=NS(symbol='SPY260317P00090000',strike_price=90.,expiration_date=date(2026,3,17))
        candidate_row=dict(symbol='SPY',entry_underlying_price=100,entry_date='2026-01-01',exit_date='2026-01-03',exit_reason='test')
        bars=[NS(timestamp=datetime(2026,1,1,tzinfo=timezone.utc),close=6.25),
              NS(timestamp=datetime(2026,1,3,tzinfo=timezone.utc),close=7)]
        with patch.object(repricer,'_contracts',return_value=[cheap,expensive]), patch.object(repricer,'_bars',return_value={SYMBOL:bars}) as get_bars:
            self.assertIsNone(repricer.reprice(candidate_row))
            self.assertEqual(repricer.last_rejection['rejection_reason'],'PREMIUM_OVER_LIMIT')
            self.assertEqual(get_bars.call_args.args[0],[SYMBOL])



class FlowRegressionTests(unittest.TestCase):
    def test_live_contract_selection_prefers_quality_before_price(self):
        expiry=date.today()+timedelta(days=75)
        cheap_symbol=f'SPY{expiry:%y%m%d}P00490000'
        preferred=NS(symbol=SYMBOL,strike_price=500,expiration_date=expiry,open_interest=1000,tradable=True)
        cheap=NS(symbol=cheap_symbol,strike_price=490,expiration_date=expiry,open_interest=1000,tradable=True)
        snapshots={SYMBOL:NS(greeks=NS(delta=-.6),latest_quote=NS(bid_price=6.24,ask_price=6.26)),
                   cheap_symbol:NS(greeks=NS(delta=-.51),latest_quote=NS(bid_price=3.99,ask_price=4.01))}
        with patch.object(trader,'get_underlying_price',return_value=495), \
             patch.object(trader.trading_client,'get_option_contracts',return_value=NS(option_contracts=[cheap,preferred],next_page_token=None)), \
             patch.object(trader,'get_option_snapshots',return_value=snapshots), \
             patch.object(trader,'get_option_volumes',return_value={SYMBOL:1000,cheap_symbol:1000}), \
             patch.object(trader,'record_event'):
            self.assertEqual(trader.get_option_contract('SPY'),SYMBOL)

    def test_fresh_candidates_survive_rejection_of_earlier_signal(self):
        import pandas as pd
        close=pd.Series([100.]*212,index=pd.bdate_range('2025-01-01',periods=212))
        with patch.object(backtester,'ENABLE_MARKET_REGIME_FILTER',False), \
             patch.object(backtester,'is_bearish_at',side_effect=lambda c,i,index:index in (205,207)), \
             patch.object(backtester,'bullish_exit_reason',return_value=''):
            candidates=backtester.backtest_close('BAC',close,collect_candidates=True)
        self.assertEqual(len(candidates),2)
        rejected=[]
        allowed=backtester.apply_portfolio_constraints(candidates,max_entry_premium=1,rejected=rejected)
        self.assertEqual(allowed,[])
        self.assertEqual(len(rejected),2)
        self.assertEqual([r['rejection_reason'] for r in rejected],['PREMIUM_OVER_LIMIT']*2)

    def test_reconciler_records_incremental_fills_once(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(analytics,'ANALYTICS_FILE',str(Path(tmp)/'ledger.csv')):
            analytics.record_event('ORDER_SUBMITTED',strategy='regular',underlying='SPY',
                option_symbol=SYMBOL,qty=1,price=4,order_id='id',order_side='buy')
            order=NS(symbol=SYMBOL,client_order_id='long_put_SPY_x',side='buy',
                     status='partially_filled',filled_qty='.5',filled_avg_price='3.9')
            with patch.object(trader.trading_client,'get_order_by_id',return_value=order), \
                 patch.object(trader.trading_client,'cancel_order_by_id') as cancel:
                trader.reconcile_order_fills()
                trader.reconcile_order_fills()
                self.assertEqual(len([r for r in analytics.read_events() if r['event']=='ORDER_FILL']),1)
                order.status='canceled';order.filled_qty='1';order.filled_avg_price='3.8'
                trader.reconcile_order_fills()
                trader.reconcile_order_fills()
                self.assertEqual(len([r for r in analytics.read_events() if r['event']=='ORDER_FILL']),2)
                self.assertEqual(analytics.get_submitted_orders(),{})
                self.assertAlmostEqual(research.capital_snapshot()['employed'],380)
                cancel.assert_not_called()

    def test_missing_inventory_recovery_preserves_owned_cost(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(analytics,'ANALYTICS_FILE',str(Path(tmp)/'ledger.csv')):
            analytics.record_event('ORDER_FILL',strategy='regular',underlying='SPY',
                option_symbol=SYMBOL,qty=1,price=4,order_side='buy')
            with patch.object(trader.trading_client,'get_all_positions',return_value=[]):
                trader.reconcile_strategy_lots_with_broker()
            self.assertEqual(research.capital_snapshot()['unresolved'],1)
            self.assertEqual(research.capital_snapshot()['employed'],400)
            with patch.object(trader.trading_client,'get_all_positions',return_value=[NS(symbol=SYMBOL,qty=1)]):
                trader.reconcile_strategy_lots_with_broker()
            self.assertEqual(research.capital_snapshot()['unresolved'],0)
            self.assertEqual(research.capital_snapshot()['employed'],400)

    def test_unrealized_loss_and_drawdown_use_owned_premium(self):
        events=[fill('buy',4,'2026-01-01T12:00:00'),
                dict(event='POSITION_SNAPSHOT',bot_strategy='long_put',strategy='',option_symbol=SYMBOL,price=3)]
        report=research.portfolio_report(events)
        self.assertEqual(report['realized_pnl'],0)
        self.assertEqual(report['unrealized_pnl'],-100)
        self.assertEqual(report['ending_virtual_capital'],24900)
        self.assertEqual(report['max_drawdown'],100)
        self.assertAlmostEqual(report['total_return_percent'],-.4)
        self.assertEqual(report['option_return_percent'],-25)

    def test_exact_historical_bar_requirement_rejects_stale_exit(self):
        repricer=AlpacaOptionRepricer()
        contract=NS(symbol=SYMBOL,strike_price=101.55,expiration_date=date(2026,3,17))
        candidate_row=dict(symbol='SPY',entry_underlying_price=100,entry_date='2026-01-01',exit_date='2026-01-03',exit_reason='test')
        bars=[NS(timestamp=datetime(2026,1,1,tzinfo=timezone.utc),close=3),
              NS(timestamp=datetime(2026,1,2,tzinfo=timezone.utc),close=4)]
        with patch.object(repricer,'_contracts',return_value=[contract]), patch.object(repricer,'_bars',return_value={SYMBOL:bars}):
            self.assertIsNone(repricer.reprice(candidate_row))
            self.assertEqual(repricer.last_rejection['details'],'missing_exact_entry_or_exit_bar')


class ConfirmedExpirationTests(unittest.TestCase):
    def test_confirmed_zero_value_expiration_closes_inventory_and_records_loss(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(analytics, 'ANALYTICS_FILE', str(Path(tmp)/'ledger.csv')):
            analytics.record_event('ORDER_FILL', strategy='regular', underlying='SPY',
                option_symbol=SYMBOL, qty=1, price=4, order_side='buy')
            analytics.record_event('EXPIRATION_CONFIRMED', strategy='regular', underlying='SPY',
                option_symbol=SYMBOL, qty=1, price=0, order_side='sell', details='broker_confirmed_worthless')
            self.assertEqual(analytics.get_strategy_open_lots(), {})
            report=research.portfolio_report(analytics.read_events())
            self.assertEqual(report['contracts_expired_worthless'], 1)
            self.assertEqual(report['premium_lost'], 400)
            self.assertEqual(report['ending_virtual_capital'], 24600)
            self.assertEqual(research.capital_snapshot()['employed'], 0)


if __name__ == '__main__':
    unittest.main()
