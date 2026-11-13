import unittest
from datetime import date

from pipelines.kensakusystem import is_session, parse_date, western_year, years_to_fetch


class EraTest(unittest.TestCase):
    def test_western_year(self):
        self.assertEqual(western_year("令和 8年"), 2026)
        self.assertEqual(western_year("令和元年 第1回定例会"), 2019)
        self.assertEqual(western_year("平成13年"), 2001)

    def test_parse_date(self):
        self.assertEqual(parse_date("開催日：令和 8年 2月17日\n会議名：…"), date(2026, 2, 17))


class TreeTest(unittest.TestCase):
    def test_session_has_name_after_year(self):
        self.assertTrue(is_session("令和 8年 第1回定例会 "))
        self.assertTrue(is_session("平成16年 予算特別委員会 "))
        self.assertFalse(is_session("令和 5年"))

    def test_years_to_fetch(self):
        self.assertEqual(years_to_fetch([2024, 2026, 2025], 2), [2026, 2025])
        self.assertEqual(years_to_fetch([2024, 2026, 2025], 0), [2026, 2025, 2024])


if __name__ == "__main__":
    unittest.main()
