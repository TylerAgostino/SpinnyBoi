# pyright: basic
"""generate_option_set filter DSL: comparison/contains/or filters, !meta
weighting/onselect/response, mixed-case headers, bad queries, empty results."""

import asyncio
import unittest
from types import SimpleNamespace
from unittest import mock

import pandas as pd

from modules.wheelCog import NoTabError, WheelCog, WheelUserError


def roster_df():
    return pd.DataFrame(
        {
            "fullname": ["Max", "Leo", "Ian"],
            "team": ["alpha", "beta", "alpha"],
            "points": [10, 3, 7],
            "active": ["yes", "no", "yes"],
        }
    )


def gen(filter_string="", frame=None):
    cog = SimpleNamespace(
        presets=SimpleNamespace(
            read_tab=mock.AsyncMock(return_value=roster_df() if frame is None else frame)
        )
    )
    return asyncio.run(WheelCog.generate_option_set(cog, "drivers", filter_string))


def summarise(options):
    return sorted((o.option, o.weight, o.on_select, o.include_text) for o in options)


class PassThroughTests(unittest.TestCase):
    def test_no_filter_returns_all_rows_with_weight_one(self):
        self.assertEqual(
            summarise(gen()),
            [("Ian", 1, None, ""), ("Leo", 1, None, ""), ("Max", 1, None, "")],
        )


class MixedCaseHeaderTests(unittest.TestCase):
    def setUp(self):
        self.frame = pd.DataFrame(
            {
                "FullName": ["Max", "Leo"],
                "Team": ["Alpha", "Beta"],
                "Points": [10, 3],
            }
        )

    def test_mixed_case_headers_and_cells_work_with_lowercase_filters(self):
        options = gen("team:alpha,!weight=points", frame=self.frame)
        self.assertEqual(
            [(o.option, o.weight) for o in options], [("Max", 10)]
        )

    def test_mixed_case_cells_match_lowercase_not_contains(self):
        options = gen("team<>beta", frame=self.frame)
        self.assertEqual([o.option for o in options], ["Max"])


class ComparisonFilterTests(unittest.TestCase):
    def test_contains_filter_matches_substring_case_insensitively(self):
        self.assertEqual([o.option for o in gen("team:ALPH")], ["Max", "Ian"])

    def test_equals_filter_matches_whole_cell_not_substring(self):
        self.assertEqual([o.option for o in gen("fullname=max")], ["Max"])
        with self.assertRaisesRegex(Exception, "returned no results"):
            gen("fullname=ma")

    def test_numeric_comparison_operators(self):
        self.assertEqual([o.option for o in gen("points>=7")], ["Max", "Ian"])
        self.assertEqual([o.option for o in gen("points>7")], ["Max"])
        self.assertEqual([o.option for o in gen("points<7")], ["Leo"])
        self.assertEqual([o.option for o in gen("points<=7")], ["Leo", "Ian"])

    def test_not_contains_filter_excludes(self):
        self.assertEqual([o.option for o in gen("team<>bet")], ["Max", "Ian"])

    def test_or_filter_union_across_pipe(self):
        self.assertEqual(
            [o.option for o in gen("team:beta|points>9")], ["Max", "Leo"]
        )

    def test_or_branch_supports_not_contains_and_gte(self):
        # <> matches Max+Ian, >=10 adds nothing new, union stays two rows
        self.assertEqual(
            [o.option for o in gen("team<>bet|points>=10")], ["Max", "Ian"]
        )

    def test_or_branch_supports_lte(self):
        # points<=3 matches only Leo; team=beta adds nothing new (Leo again)
        self.assertEqual([o.option for o in gen("points<=3|team=beta")], ["Leo"])

    def test_or_branch_supports_strict_less_than(self):
        # points<3 matches nobody (Leo has exactly 3); points=10 adds Max
        self.assertEqual([o.option for o in gen("points<3|points=10")], ["Max"])

    def test_or_branch_equals_is_whole_cell_case_insensitive(self):
        # a partial operand must NOT match under '='; case is ignored
        self.assertEqual([o.option for o in gen("team=bet|points>9")], ["Max"])
        self.assertEqual([o.option for o in gen("team=BETA")], ["Leo"])

    def test_comma_combines_filters_anded(self):
        self.assertEqual([o.option for o in gen("team:alpha,points>8")], ["Max"])


class MetaDirectiveTests(unittest.TestCase):
    def test_weight_column_drives_option_weights(self):
        self.assertEqual(
            [(o.option, o.weight) for o in gen("!weight=points")],
            [("Max", 10), ("Leo", 3), ("Ian", 7)],
        )

    def test_onselect_and_response_columns_attach_to_options(self):
        options = gen("!onselect=team,!response=active")
        by_name = {o.option: o for o in options}
        self.assertEqual(by_name["Max"].on_select, "alpha")
        self.assertEqual(by_name["Max"].include_text, "yes")
        self.assertEqual(by_name["Leo"].on_select, "beta")
        self.assertEqual(by_name["Leo"].include_text, "no")

    def test_meta_and_filter_combine(self):
        options = gen("team:alpha,!weight=points")
        self.assertEqual(
            [(o.option, o.weight) for o in options], [("Max", 10), ("Ian", 7)]
        )


class FailureTests(unittest.TestCase):
    def test_missing_tab_raises_no_tab_error(self):
        empty = pd.DataFrame(columns=["FALSE"])
        with self.assertRaisesRegex(NoTabError, "drivers"):
            gen(frame=empty)

    def test_read_tab_exception_is_sanitized(self):
        boom = SimpleNamespace(
            read_tab=mock.AsyncMock(side_effect=RuntimeError("sheet api down"))
        )
        cog = SimpleNamespace(presets=boom)
        with self.assertRaisesRegex(
            RuntimeError, r"I couldn't read the sheet tab 'drivers'"
        ) as ctx:
            asyncio.run(WheelCog.generate_option_set(cog, "drivers", ""))
        self.assertNotIn("sheet api down", str(ctx.exception))

    def test_filter_on_unknown_column_reports_bad_filter(self):
        with self.assertRaisesRegex(Exception, "Something is wrong"):
            gen("nosuchcolumn:1")

    def test_empty_result_raises_named_filter_message(self):
        with self.assertRaisesRegex(Exception, "returned no results"):
            gen("points>1000")

    def test_malformed_or_query_raises_wheel_user_error_and_logs_query(self):
        # A dangling ">=" operand parses into a query pandas rejects; the
        # user gets the fixed reply, the log keeps the offending query.
        with self.assertLogs(level="ERROR") as logs:
            with self.assertRaisesRegex(
                WheelUserError,
                r"^Something is wrong with one of your filters\.$",
            ):
                gen("points>=|team:alpha")
        joined = "\n".join(logs.output)
        self.assertIn("Error processing filter", joined)
        self.assertIn("`points`>=", joined)


if __name__ == "__main__":
    unittest.main()
