# pyright: basic
"""Tests for the RegistrationRoster module: pure sweep rules and the
registration business rules, exercised through the roster interface with
the fake Google service from test_sheets_client."""
import unittest
from typing import Optional

from modules.registration_roster import (
    RegistrationRoster,
    match_user,
    penalty_prefix,
    registration_status,
)
from modules.sheets_client import SheetsError
from tests.test_sheets_client import _client


class PenaltyPrefixTests(unittest.TestCase):
    def test_boundaries(self):
        cases = [
            (0, ""), (2, ""), ("", ""), (None, ""), ("abc", ""),
            (3, "(X)"), (5, "(X)"),
            (6, "(XX)"), (8, "(XX)"),
            (9, "(XXX)"), (11, "(XXX)"),
            (12, "(XXXX)"), ("7", "(XX)"),
        ]
        for points, expected in cases:
            self.assertEqual(penalty_prefix(points), expected, msg=str(points))


def _driver(iRacingID, **extra) -> dict:
    row = {"iRacingID": iRacingID, "RegistrationDate": "2026-01-01"}
    row.update(extra)
    return row


class RegistrationStatusTests(unittest.TestCase):
    def test_missing_from_league_needs_invite(self):
        status = registration_status([_driver("10", DesiredName="Al")], [], [])
        self.assertEqual(status.invites, ["Al"])
        self.assertFalse(status.name_changes)

    def test_pending_invite_needs_accept(self):
        pending = [{"cust_id": 10}]
        status = registration_status([_driver("10", DesiredName="Al")], [], pending)
        self.assertEqual(status.accept_invites, ["Al"])
        self.assertFalse(status.invites)

    def test_unregistered_rows_are_skipped(self):
        row = {"iRacingID": "10", "DesiredName": "Al"}  # no RegistrationDate
        status = registration_status([row], [], [])
        self.assertFalse(status.invites)

    def test_registered_row_without_iracing_id_needs_no_action(self):
        # A blank iRacing id cannot be reconciled with the league: the row
        # is skipped entirely, not invited.
        users = [
            _driver("", DesiredName="Al"),
            _driver("  ", DesiredName="Cy"),
            _driver("10", DesiredName="Bo"),
        ]
        status = registration_status(users, [], [])
        self.assertEqual(status.invites, ["Bo"])
        self.assertFalse(status.accept_invites)
        self.assertFalse(status.name_changes)

    def test_wrong_base_name_is_name_change(self):
        users = [_driver("10", DesiredName="Al", CarNumber="7")]
        members = [{"cust_id": 10, "nick_name": "Buddy", "car_number": "7"}]
        status = registration_status(users, members, [])
        self.assertEqual(status.name_changes, [("Buddy", "Al")])
        self.assertFalse(status.penalty_prefix_changes)

    def test_right_name_wrong_prefix_is_prefix_change(self):
        users = [_driver("10", DesiredName="Al", CarNumber="7", PenaltyPoints=6)]
        members = [{"cust_id": 10, "nick_name": "Al", "car_number": "7"}]
        status = registration_status(users, members, [])
        self.assertEqual(status.penalty_prefix_changes, [("Al", "Al", "(XX) Al")])
        self.assertFalse(status.name_changes)

    def test_existing_wrong_prefix_stripped_before_compare(self):
        users = [_driver("10", DesiredName="Al", CarNumber="7", PenaltyPoints=6)]
        members = [{"cust_id": 10, "nick_name": "(X) Al", "car_number": "7"}]
        status = registration_status(users, members, [])
        self.assertEqual(status.penalty_prefix_changes, [("Al", "(X) Al", "(XX) Al")])

    def test_car_number_mismatch(self):
        users = [_driver("'10", DesiredName="Al", CarNumber="7")]
        members = [{"cust_id": 10, "nick_name": "Al", "car_number": "8"}]
        status = registration_status(users, members, [])
        self.assertEqual(status.number_changes, [("Al", "7", "8")])

    def test_fully_conforming_driver_yields_no_actions(self):
        users = [_driver("10", DesiredName="Al", CarNumber="7")]
        members = [{"cust_id": 10, "display_name": "Al", "car_number": "7"}]
        status = registration_status(users, members, [])
        self.assertEqual(
            (
                status.name_changes,
                status.number_changes,
                status.invites,
                status.accept_invites,
                status.penalty_prefix_changes,
            ),
            ([], [], [], [], []),
        )


def _store() -> dict:
    return {
        "Users": [
            ["DiscordID", "iRacingID", "DesiredName", "CarNumber", "NumRaces"],
        ]
    }


def _roster(store) -> RegistrationRoster:
    return RegistrationRoster(_client(store), "sid", "Users")


def _name_ok(cust_id: int) -> Optional[str]:
    return f"Driver {cust_id}"


class RosterRegisterDriverTests(unittest.TestCase):
    def test_invalid_ids_rejected_before_any_write(self):
        store = _store()
        roster = _roster(store)
        with self.assertRaises(ValueError):
            roster.register_driver(1, "123456789", "Al", "7", "9", _name_ok)
        with self.assertRaises(ValueError):
            roster.register_driver(1, "10", "Al", "abc", "9", _name_ok)
        self.assertEqual(len(store["Users"]), 1)  # header only

    def test_unknown_customer_rejected(self):
        store = _store()
        roster = _roster(store)
        with self.assertRaises(ValueError):
            roster.register_driver(
                1, "10", "Al", "7", "9", lambda cid: None
            )
        self.assertEqual(len(store["Users"]), 1)

    def test_success_writes_quoted_row_and_updates_existing(self):
        store = _store()
        roster = _roster(store)
        roster.register_driver(1, "10", "Al", "7", "9", _name_ok)
        row = store["Users"][1]
        self.assertEqual(row, ["1", "10", "Al", "7", "9"])
        # second call with same DiscordID updates the same row
        roster.register_driver(1, "10", "Al Renamed", "7", "12", _name_ok)
        self.assertEqual(len(store["Users"]), 2)
        self.assertEqual(store["Users"][1][2], "Al Renamed")
        self.assertEqual(store["Users"][1][4], "12")

    def test_taken_number_records_and_raises(self):
        store = {
            "Users": [
                ["DiscordID", "iRacingID", "DesiredName", "CarNumber",
                 "DesiredButTakenCarNums"],
            ]
        }
        roster = _roster(store)
        roster.register_driver(1, "10", "Al", "7", "9", _name_ok)
        with self.assertRaises(ValueError):
            roster.register_driver(2, "20", "Bo", "7", "9", _name_ok)
        row = store["Users"][2]
        self.assertEqual(row[0], "2")
        self.assertEqual(row[4], "7")  # "'7" normalized by the sheet on write

    def test_taken_number_appends_to_existing_user_row(self):
        store = {
            "Users": [
                ["DiscordID", "iRacingID", "DesiredName", "CarNumber",
                 "DesiredButTakenCarNums"],
                ["1", "10", "Al", "", "'3"],
            ]
        }
        roster = _roster(store)
        # someone else holds number 7 now
        store["Users"].append(["2", "20", "Bo", "7", ""])
        with self.assertRaises(ValueError):
            roster.register_driver(1, "10", "Al", "7", "9", _name_ok)
        self.assertEqual(len(store["Users"]), 3)  # row updated, not appended
        self.assertEqual(store["Users"][1][:2], ["1", "10"])
        # sheet strips the leading apostrophe on write (USER_ENTERED semantics)
        self.assertEqual(store["Users"][1][4], "7, '3")

    def test_match_user_handles_quoted_ids(self):
        users = [{"DiscordID": "'5"}, {"DiscordID": 6}]
        self.assertEqual(match_user(users, 5)["DiscordID"], "'5")
        self.assertEqual(match_user(users, 6)["DiscordID"], 6)
        self.assertIsNone(match_user(users, 7))

    def test_missing_sheet_raises_on_write(self):
        roster = RegistrationRoster(_client({}), None, "Users")
        with self.assertRaises(SheetsError):
            roster.add({"DiscordID": "1"})
        self.assertEqual(roster.read_users(), [])


if __name__ == "__main__":
    unittest.main()
