"""NYSE/NASDAQ holiday rules. A weekend session is not added on top of the observed day."""

import unittest

from market_holidays import get_market_holidays, is_market_holiday


class MarketHolidayTest(unittest.TestCase):
    def test_2026_full_closes(self):
        holidays = get_market_holidays(2026)
        expected = {
            "2026-01-01",  # New Year's Day
            "2026-01-19",  # MLK Day
            "2026-02-16",  # Presidents Day
            "2026-04-03",  # Good Friday
            "2026-05-25",  # Memorial Day
            "2026-06-19",  # Juneteenth
            "2026-07-03",  # Independence Day, observed (July 4 is Saturday)
            "2026-09-07",  # Labor Day
            "2026-11-26",  # Thanksgiving
            "2026-12-25",  # Christmas
        }
        self.assertEqual(holidays, expected)

    def test_saturday_independence_day_is_observed_friday(self):
        holidays = get_market_holidays(2026)
        self.assertIn("2026-07-03", holidays)
        self.assertNotIn("2026-07-04", holidays)
        self.assertTrue(is_market_holiday("2026-07-03"))
        self.assertFalse(is_market_holiday("2026-07-04"))

    def test_sunday_holiday_moves_to_monday(self):
        # July 4, 2021 was a Sunday. The session closed on Monday July 5.
        holidays = get_market_holidays(2021)
        self.assertIn("2021-07-05", holidays)
        self.assertNotIn("2021-07-04", holidays)

    def test_regular_session_is_not_a_holiday(self):
        self.assertFalse(is_market_holiday("2026-10-16"))
        self.assertFalse(is_market_holiday("2026-03-24"))


if __name__ == "__main__":
    unittest.main()
