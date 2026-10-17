from types import SimpleNamespace
import json
from pathlib import Path
import unittest
from unittest.mock import ANY, Mock, patch

from trade_bot import ExecutionPolicy, Journal, MT5Broker, entry_zone, execution_policy, parse_signal, process_update


SAMPLE = (Path(__file__).parent / "fixtures" / "xau_buy.txt").read_text(encoding="utf-8")


class TradeTests(unittest.TestCase):
    def setUp(self):
        self.journal = Journal(":memory:")
        self.addCleanup(self.journal.db.close)
        self.update = {"message": {"chat": {"id": -1001}, "message_id": 7,
                                   "date": 1000, "text": "BUY EURUSD ENTRY 1.10 SL 1.09 TP 1.12"}}

    def test_parse_buy_sell_and_preserve_broker_suffix(self):
        self.assertEqual(parse_signal("BUY EURUSD.m ENTRY 1.10 SL 1.09 TP 1.12").symbol, "EURUSD.m")
        self.assertEqual(parse_signal("sell XAUUSD\nENTRY 3000\nSL 3010\nTP 2980").side, "SELL")

    def test_actual_gold_signal_preserves_range_targets_and_risk(self):
        signal = parse_signal(SAMPLE)
        self.assertEqual((signal.symbol, signal.side), ("XAUUSD", "BUY"))
        self.assertEqual((signal.entry_low, signal.entry_high, signal.sl), (4169, 4172, 4166))
        self.assertEqual(signal.targets, (4182, 4198, 4207))
        self.assertTrue(signal.open_target)
        self.assertEqual(signal.risk_percent, (.5, 1))

    def test_zone_shorthand_full_prices_and_single_price(self):
        for value, expected in (("4172-69", (4169, 4172)), ("4169–4172", (4169, 4172)),
                                ("4172", (4172, 4172)), ("1.1050-40", (1.1040, 1.1050))):
            self.assertEqual(entry_zone(value), expected)
        with self.assertRaises(ValueError):
            entry_zone("4172.00-69")

    def test_sell_zone_and_single_risk_percentage(self):
        text = "XAUUSD SELL ZONE\nEntry: 4172-69\nSL: 4176\nTP1: 4160\nTP2: 4150\nRisk: 0.5%"
        signal = parse_signal(text)
        self.assertEqual(signal.side, "SELL")
        self.assertEqual(signal.targets, (4160, 4150))
        self.assertEqual(signal.risk_percent, (.5, .5))

    def test_zone_rejects_malformed_targets_stops_and_risk(self):
        mutations = (("TP2: 4198", "TP3: 4198"), ("TP2: 4198", "TP2: 4180"),
                     ("Stoploss: 4166", "Stoploss: 4170"), ("TP1: 4182", "TP1: 4171"),
                     ("TP3: 4207", "TP3: OPEN"), ("0.5-1%", "1-0.5%"),
                     ("0.5-1%", "0%"), ("0.5-1%", "101%"),
                     ("TP4: OPEN", "TP4: SOMEDAY"))
        for old, new in mutations:
            with self.subTest(new=new), self.assertRaises(ValueError):
                parse_signal(SAMPLE.replace(old, new))
        with self.assertRaises(ValueError):
            parse_signal(SAMPLE + "\nBUY ANOTHER SYMBOL")

    def test_paper_zone_signal_is_journaled_with_all_fields(self):
        self.update["message"]["text"] = SAMPLE
        self.assertEqual(process_update(self.update, {-1001}, self.journal, now=1001), "paper")
        payload = json.loads(self.journal.db.execute("SELECT payload FROM signals").fetchone()[0])
        self.assertEqual(payload["targets"], [4182, 4198, 4207])
        self.assertEqual(payload["entry_low"], 4169)
        self.assertEqual(payload["risk_percent"], [.5, 1])

    def test_reject_ambiguous_or_inverted_signals(self):
        for text in ("BUY EURUSD", "BUY EURUSD ENTRY 1.1 SL 1.2 TP 1.3",
                     "SELL EURUSD ENTRY 1.1 SL 1.2 TP 1.3",
                     "BUY EURUSD ENTRY 1.1-1.2 SL 1.0 TP 1.3",
                     "BUY EURUSD ENTRY 1.1 SL 1.0 TP 1.2 TP 1.3"):
            with self.subTest(text=text), self.assertRaises(ValueError):
                parse_signal(text)

    def test_paper_never_submits_and_duplicate_is_ignored(self):
        self.assertEqual(process_update(self.update, {-1001}, self.journal, now=1001), "paper")
        broker = Mock()
        self.assertEqual(process_update(self.update, {-1001}, self.journal, broker, now=1001), "duplicate")
        broker.prepare.assert_not_called()
        broker.execute.assert_not_called()

    def test_excluded_stale_future_and_edited_messages_never_trade(self):
        broker = Mock()
        for sources, now, update in (({-1002}, 1001, self.update), ({-1001}, 1121, self.update),
                                     ({-1001}, 999, self.update), ({-1001}, 1001, {"edited_message": self.update["message"]})):
            process_update(update, sources, self.journal, broker, now=now)
        broker.prepare.assert_not_called()
        broker.execute.assert_not_called()

    def test_uncertain_order_is_not_retried(self):
        broker = Mock()
        broker.execute.side_effect = RuntimeError("uncertain")
        with self.assertRaises(RuntimeError):
            process_update(self.update, {-1001}, self.journal, broker, now=1001)
        self.assertEqual(process_update(self.update, {-1001}, self.journal, broker, now=1001), "duplicate")
        broker.execute.assert_called_once()
        self.assertEqual(self.journal.db.execute("SELECT status FROM messages").fetchone()[0], "submitting")

    def test_successful_order_and_broker_rejection(self):
        broker = Mock()
        self.assertEqual(process_update(self.update, {-1001}, self.journal, broker, now=1001), "executed")
        broker.execute.assert_called_once_with(broker.prepare.return_value, record=ANY)
        self.update["message"]["message_id"] = 8
        broker.prepare.side_effect = ValueError("invalid volume")
        self.assertEqual(process_update(self.update, {-1001}, self.journal, broker, now=1001), "rejected")
        self.assertEqual(broker.execute.call_count, 1)


class BrokerTests(unittest.TestCase):
    def setUp(self):
        self.mt5 = Mock(TRADE_ACTION_DEAL=1, ORDER_TYPE_BUY=0, ORDER_TYPE_SELL=1,
                        ORDER_TIME_GTC=0, ORDER_FILLING_FOK=0, TRADE_RETCODE_DONE=10009,
                        ACCOUNT_MARGIN_MODE_RETAIL_HEDGING=2)
        self.mt5.account_info.return_value = SimpleNamespace(login=123, trade_allowed=True, trade_expert=True, margin_mode=2)
        self.mt5.terminal_info.return_value = SimpleNamespace(connected=True, trade_allowed=True)
        self.mt5.symbol_info.return_value = SimpleNamespace(point=.00001, volume_step=.01,
                                                           volume_min=.01, volume_max=100, filling_mode=1)
        self.mt5.symbol_info_tick.return_value = SimpleNamespace(ask=1.1, bid=1.0999, time=1000)
        self.mt5.order_check.return_value = SimpleNamespace(retcode=0)
        self.mt5.order_send.return_value = SimpleNamespace(retcode=10009, order=101, deal=201)
        self.signal = parse_signal("BUY EURUSD ENTRY 1.10 SL 1.09 TP 1.12")

    def test_request_contains_size_direction_and_stops(self):
        broker = MT5Broker(self.mt5, 123, .02)
        with patch("trade_bot.time.time", return_value=1001):
            plan = broker.prepare(self.signal)
            request = plan.requests[0]
            self.assertEqual((request["volume"], request["sl"], request["tp"], request["type"]), (.02, 1.09, 1.12, 0))
            self.mt5.order_send.assert_not_called()
            broker.execute(plan)
        self.mt5.order_send.assert_called_once_with(request)

    def test_multi_target_risk_signal_cannot_silently_become_one_fixed_lot_order(self):
        broker = MT5Broker(self.mt5, 123, .02)
        with self.assertRaisesRegex(ValueError, "MT5_TARGET_LOTS"):
            broker.prepare(parse_signal(SAMPLE))
        self.mt5.order_check.assert_not_called()
        self.mt5.order_send.assert_not_called()

    def test_single_target_zone_requires_price_inside_zone(self):
        signal = parse_signal("EURUSD BUY ZONE\nEntry: 1.1000-1.1005\nSL: 1.09\nTP1: 1.12")
        broker = MT5Broker(self.mt5, 123, .02)
        with patch("trade_bot.time.time", return_value=1001):
            self.assertEqual(broker.prepare(signal).requests[0]["price"], 1.1)
            self.mt5.symbol_info_tick.return_value.ask = 1.0999
            with self.assertRaisesRegex(ValueError, "outside the signal entry zone"):
                broker.prepare(signal)

    def test_wrong_account_and_invalid_volume_fail(self):
        with self.assertRaises(RuntimeError):
            MT5Broker(self.mt5, 456, .01)
        for lots in (0, -1, float("nan"), float("inf")):
            with self.assertRaises(ValueError):
                MT5Broker(self.mt5, 123, lots)
        broker = MT5Broker(self.mt5, 123, .015)
        with patch("trade_bot.time.time", return_value=1001), self.assertRaises(ValueError):
            broker.prepare(self.signal)

    def test_stale_quote_price_drift_and_rejected_check_fail(self):
        broker = MT5Broker(self.mt5, 123, .01)
        with patch("trade_bot.time.time", return_value=1031), self.assertRaises(ValueError):
            broker.prepare(self.signal)
        with patch("trade_bot.time.time", return_value=1001):
            self.mt5.symbol_info_tick.return_value.ask = 1.2
            with self.assertRaises(ValueError):
                broker.prepare(self.signal)
            self.mt5.symbol_info_tick.return_value.ask = 1.1
            self.mt5.order_check.return_value.retcode = 10019
            with self.assertRaises(ValueError):
                broker.prepare(self.signal)
        self.mt5.order_send.assert_not_called()

    def gold_broker(self):
        self.mt5.symbol_info_tick.return_value = SimpleNamespace(ask=4170, bid=4169.9, time=1000)
        return MT5Broker(self.mt5, 123, .1, (.05, .03, .02))

    def test_gold_buy_split_is_total_point_one_with_no_open_position(self):
        broker = self.gold_broker()
        with patch("trade_bot.time.time", return_value=1001):
            plan = broker.prepare(parse_signal(SAMPLE))
            self.assertEqual([(r["volume"], r["tp"]) for r in plan.requests], [(.05, 4182), (.03, 4198), (.02, 4207)])
            self.assertTrue(all(r["sl"] == 4166 and r["type"] == 0 for r in plan.requests))
            self.assertEqual(self.mt5.order_check.call_count, 3)
            self.mt5.order_send.assert_not_called()
            broker.execute(plan)
        self.assertEqual(self.mt5.order_send.call_count, 3)

    def test_gold_sell_uses_same_fixed_split_and_ignores_risk_percentage(self):
        broker = self.gold_broker()
        text = "XAUUSD SELL ZONE\nEntry: 4172-69\nSL: 4176\nTP1: 4160\nTP2: 4150\nTP3: 4140\nTP4: OPEN\nRisk: 1%"
        with patch("trade_bot.time.time", return_value=1001):
            plan = broker.prepare(parse_signal(text))
        self.assertEqual([(r["volume"], r["tp"], r["type"]) for r in plan.requests], [(.05, 4160, 1), (.03, 4150, 1), (.02, 4140, 1)])

    def test_split_requires_hedging_and_valid_per_leg_volume(self):
        broker = self.gold_broker()
        self.mt5.account_info.return_value.margin_mode = 0
        with self.assertRaisesRegex(ValueError, "hedging"):
            broker.prepare(parse_signal(SAMPLE))
        self.mt5.account_info.return_value.margin_mode = 2
        self.mt5.symbol_info.return_value.volume_min = .1
        with patch("trade_bot.time.time", return_value=1001), self.assertRaisesRegex(ValueError, "allowed range"):
            broker.prepare(parse_signal(SAMPLE))
        self.mt5.order_send.assert_not_called()

    def test_any_failed_preflight_prevents_all_sends(self):
        broker = self.gold_broker()
        self.mt5.order_check.side_effect = [SimpleNamespace(retcode=0), SimpleNamespace(retcode=10019)]
        with patch("trade_bot.time.time", return_value=1001), self.assertRaises(ValueError):
            broker.prepare(parse_signal(SAMPLE))
        self.mt5.order_send.assert_not_called()

    def test_partial_batch_stops_and_journals_results_without_retry(self):
        broker = self.gold_broker()
        self.mt5.order_send.side_effect = [SimpleNamespace(retcode=10009, order=101, deal=201),
                                         SimpleNamespace(retcode=10019, order=0, deal=0)]
        update = {"message": {"chat": {"id": -1001}, "message_id": 7, "date": 1000, "text": SAMPLE}}
        journal = Journal(":memory:")
        self.addCleanup(journal.db.close)
        with patch("trade_bot.time.time", return_value=1001), self.assertRaises(RuntimeError):
            process_update(update, {-1001}, journal, broker, now=1001)
        self.assertEqual(self.mt5.order_send.call_count, 2)
        rows = journal.db.execute("SELECT leg, result FROM executions ORDER BY leg").fetchall()
        self.assertEqual([(leg, json.loads(result)["retcode"]) for leg, result in rows], [(1, 10009), (2, 10019)])
        self.assertEqual(process_update(update, {-1001}, journal, broker, now=1001), "duplicate")
        self.assertEqual(self.mt5.order_send.call_count, 2)

    def test_quote_moving_outside_zone_before_send_prevents_order(self):
        broker = self.gold_broker()
        with patch("trade_bot.time.time", return_value=1001):
            plan = broker.prepare(parse_signal(SAMPLE))
            self.mt5.symbol_info_tick.return_value.ask = 4173
            with self.assertRaises(ValueError):
                broker.execute(plan)
        self.mt5.order_send.assert_not_called()


class PolicyTests(unittest.TestCase):
    def test_user_defaults_allocate_point_one_to_three_targets(self):
        policy = execution_policy({})
        self.assertEqual(policy.total_lots, .1)
        self.assertEqual(policy.allocations(parse_signal(SAMPLE)), ((.05, 4182), (.03, 4198), (.02, 4207)))

    def test_invalid_split_fails_without_rounding_or_redistribution(self):
        for split in ((.05, .03, .03), (.1, 0), (.05, float("nan")), (.05, -.05, .1)):
            with self.subTest(split=split), self.assertRaises(ValueError):
                ExecutionPolicy(.1, split)
        with self.assertRaises(ValueError):
            ExecutionPolicy(.1, (.05, .05)).allocations(parse_signal(SAMPLE))

    def test_explicit_open_allocation_and_single_target(self):
        policy = ExecutionPolicy(.1, (.03, .03, .02, .02))
        self.assertEqual(policy.allocations(parse_signal(SAMPLE))[-1], (.02, 0))
        single = parse_signal("BUY EURUSD ENTRY 1.1 SL 1.0 TP 1.2")
        self.assertEqual(execution_policy({}).allocations(single), ((.1, 1.2),))
