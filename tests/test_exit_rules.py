"""Exit table used by the paper books. Stop is flat. One cell changes with the hold."""

import unittest

from stock_drop_exit_rules import (
    STANDARD_SL_PCT,
    numbered_watchlist_blocked,
    resolve_take_profit,
    standard_take_profit,
)


class ExitRulesTest(unittest.TestCase):
    def test_stop_is_flat(self):
        tp, sl, _label = standard_take_profit(5.5, 18, hold_days=10)
        self.assertEqual(tp, 150)
        self.assertEqual(sl, STANDARD_SL_PCT)
        self.assertEqual(sl, -40)

    def test_five_to_six_keeps_target_on_both_holds(self):
        for hold in (10, 20):
            tp, _sl, _label = standard_take_profit(5.2, 15, hold_days=hold)
            self.assertEqual(tp, 150)
            tp, _sl, _label = standard_take_profit(5.2, 22, hold_days=hold)
            self.assertEqual(tp, 150)

    def test_five_to_six_holds_when_vix_is_30_or_higher(self):
        tp, _sl, label = standard_take_profit(5.2, 31, hold_days=10)
        self.assertIsNone(tp)
        self.assertIn("hold", label)

    def test_mid_buckets_hold_the_window(self):
        for drop, vix in ((7, 15), (7, 24), (9, 18), (12, 22), (16, 27)):
            tp, _sl, _label = standard_take_profit(drop, vix, hold_days=10)
            self.assertIsNone(tp)

    def test_ten_to_fifteen_under_20_splits_by_hold(self):
        tp10, _sl, _label = standard_take_profit(12, 15, hold_days=10)
        tp20, _sl, _label = standard_take_profit(12, 15, hold_days=20)
        self.assertIsNone(tp10)
        self.assertEqual(tp20, 150)

    def test_fifteen_plus_under_20_takes_profit(self):
        tp, _sl, _label = standard_take_profit(18, 12, hold_days=10)
        self.assertEqual(tp, 150)

    def test_drop_below_five_percent_has_no_target(self):
        self.assertEqual(
            standard_take_profit(4.9, 15, hold_days=10),
            (None, -40, "no tier — hold"),
        )
        self.assertIsNone(standard_take_profit("n/a", 15))

    def test_watchlist_cap_blocks_elevated_vix_except_panic(self):
        self.assertTrue(numbered_watchlist_blocked(5.5, 22))
        self.assertFalse(numbered_watchlist_blocked(5.5, 19))
        self.assertFalse(numbered_watchlist_blocked(5.5, 35))
        self.assertFalse(numbered_watchlist_blocked(7, 28))
        self.assertTrue(numbered_watchlist_blocked(9, 22))
        self.assertTrue(numbered_watchlist_blocked(4, 15))

    def test_tier_label_supplies_vix_when_the_quote_is_missing(self):
        tp10, _sl, _label = resolve_take_profit(
            12, tier="10-15%|VIX<20", hold_days=10)
        tp20, _sl, _label = resolve_take_profit(
            12, tier="10-15%|VIX<20", hold_days=20)
        self.assertIsNone(tp10)
        self.assertEqual(tp20, 150)
        self.assertIsNone(resolve_take_profit(12, tier="not a tier"))


if __name__ == "__main__":
    unittest.main()
