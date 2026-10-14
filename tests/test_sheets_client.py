"""Tests for the SheetsClient seam and the cogs driving it through it.

A FakeSheetsService implements just enough of the googleapiclient fluent
surface (values().get/append/update/batchUpdate, USER_ENTERED quote
stripping) to run the real client logic without Google. If these tests
pass, the registration and attendance write paths work end to end through
the seam — that interface IS the test surface.
"""
import os
import re
import unittest
from typing import Dict, List

from modules.sheets_client import SheetsClient, SheetsError, strip_id_quote


def _col_letter_to_index(letter: str) -> int:
    idx = 0
    for ch in letter:
        idx = idx * 26 + (ord(ch) - ord("A") + 1)
    return idx - 1


def _parse_range(range_name: str):
    """Parse \"'Sheet'!A1:ZZ\" into (sheet, start_row, start_col, end_row)."""
    sheet_part, cell_part = range_name.rsplit("!", 1)
    sheet = sheet_part.strip("'")
    m = re.fullmatch(r"([A-Z]+)(\d+)(?::[A-Z]+(\d+)?)?", cell_part)
    if not m:  # whole-column range, e.g. "A:Z"
        m2 = re.fullmatch(r"([A-Z]+):([A-Z]+)", cell_part)
        assert m2, f"unsupported range {range_name}"
        return sheet, 1, _col_letter_to_index(m2.group(1)), None
    col = _col_letter_to_index(m.group(1))
    row = int(m.group(2))
    end_row = int(m.group(3)) if m.group(3) else None
    return sheet, row, col, end_row


def _normalize(value) -> str:
    # USER_ENTERED semantics: a leading apostrophe forces text and is not
    # stored, so reads never see it.
    return strip_id_quote(value)


class FakeValues:
    def __init__(self, store: Dict[str, List[List[str]]]):
        self.store = store
        self.calls: List[str] = []

    def get(self, spreadsheetId, range):  # noqa: N803
        self.calls.append(f"get:{range}")
        sheet, row, _col, end_row = _parse_range(range)
        rows = self.store.get(sheet, [])
        if end_row is not None:
            rows = rows[row - 1 : end_row]
        else:
            rows = rows[row - 1 :]
        return _Call(self, {"values": rows})

    def append(self, spreadsheetId, range, valueInputOption, insertDataOption, body):  # noqa: N803
        self.calls.append(f"append:{range}")
        sheet, _row, _col, _end = _parse_range(range)
        rows = self.store.setdefault(sheet, [])
        for row in body["values"]:
            rows.append([_normalize(v) for v in row])
        return _Call(self, {"updates": {"updatedRows": len(body["values"])}})

    def update(self, spreadsheetId, range, valueInputOption, body):  # noqa: N803
        self.calls.append(f"update:{range}")
        sheet, row, _col, _end = _parse_range(range)
        rows = self.store.setdefault(sheet, [])
        target = row - 1
        new_row = [_normalize(v) for v in body["values"][0]]
        while len(rows) <= target:
            rows.append([])
        rows[target] = new_row
        return _Call(self, {"updates": {"updatedRows": 1}})

    def batchUpdate(self, spreadsheetId, body):  # noqa: N803
        data = body["data"]
        self.calls.append(f"batchUpdate:{len(data)}")
        for cell in data:
            sheet, row, col, _end = _parse_range(cell["range"])
            rows = self.store.setdefault(sheet, [])
            target = row - 1
            while len(rows) <= target:
                rows.append([])
            while len(rows[target]) <= col:
                rows[target].append("")
            rows[target][col] = _normalize(cell["values"][0][0])
        return _Call(self, {"totalUpdatedCells": len(data)})


class _Call:
    def __init__(self, values, result):
        self._values = values
        self._result = result

    def execute(self):
        return self._result


class FakeSheetsService:
    def __init__(self, store=None):
        self.store: Dict[str, List[List[str]]] = store if store is not None else {}
        self._fake_values = FakeValues(self.store)

    def spreadsheets(self):
        return self

    def values(self):
        return self._fake_values


def _client(store=None) -> SheetsClient:
    client = SheetsClient(credentials=object())
    client._service = FakeSheetsService(store)
    return client


REG_STORE = {
    "Users": [
        ["DiscordID", "iRacingID", "DesiredName", "CarNumber"],
        ["111", "222", "Old Name", "7"],
        ["333", "444", "Keep", "8"],
    ]
}


class SheetsClientTests(unittest.TestCase):
    def test_unavailable_client_raises_on_use(self):
        client = SheetsClient(credentials=None)
        self.assertFalse(client.available)
        with self.assertRaises(SheetsError):
            client.read_grid("sid", "Users")

    def test_read_rows_zips_and_pads(self):
        client = _client({"S": [["A", "B", "C"], ["1", "2"]]})
        rows = client.read_rows("sid", "S")
        self.assertEqual(
            rows, [{"A": "1", "B": "2", "C": ""}]
        )

    def test_read_grid_raises_on_empty_sheet(self):
        client = _client({})
        with self.assertRaises(SheetsError):
            client.read_grid("sid", "Nope")

    def test_append_record_orders_by_headers_and_fills_missing(self):
        store = {"S": [["A", "B", "C"]]}
        client = _client(store)
        client.append_record("sid", "S", {"C": "c!", "A": "a!"})
        self.assertEqual(store["S"][1], ["a!", "", "c!"])

    def test_update_record_merges_and_preserves_other_cells(self):
        store = {
            "Users": [
                ["DiscordID", "iRacingID", "DesiredName"],
                ["111", "222", "Old"],
            ]
        }
        client = _client(store)
        client.update_record("sid", "Users", "DiscordID", 111, {"DesiredName": "New"})
        self.assertEqual(store["Users"][1], ["111", "222", "New"])

    def test_update_record_matches_quoted_ids(self):
        store = {"Users": [["DiscordID", "Name"], ["'555", "x"]]}
        client = _client(store)
        client.update_record("sid", "Users", "DiscordID", 555, {"Name": "y"})
        self.assertEqual(store["Users"][1], ["555", "y"])

    def test_update_record_missing_key_raises(self):
        client = _client({"Users": [["Name"], ["x"]]})
        with self.assertRaises(SheetsError):
            client.update_record("sid", "Users", "DiscordID", 1, {})

    def test_update_record_missing_row_raises(self):
        client = _client(
            {"Users": [["DiscordID", "N"], ["1", "a"]]}
        )
        with self.assertRaises(SheetsError):
            client.update_record("sid", "Users", "DiscordID", 999, {"N": "z"})

    def test_update_column_cells_writes_by_header(self):
        store = {
            "Points": [["iRacing ID", "S17R1", "S17R2"], ["10", "", ""], ["20", "", ""]]
        }
        client = _client(store)
        written = client.update_column_cells("sid", "Points", "S17R2", {2: 0, 3: 0})
        self.assertEqual(written, 2)
        self.assertEqual(store["Points"][1], ["10", "", "0"])
        self.assertEqual(store["Points"][2], ["20", "", "0"])

    def test_update_column_cells_unknown_header_raises(self):
        client = _client({"Points": [["A"]]})
        with self.assertRaises(SheetsError):
            client.update_column_cells("sid", "Points", "Nope", {2: 0})


class AttendanceThroughSeamTests(unittest.TestCase):
    def _cog(self, store):
        from modules.attendanceCog import AttendanceCog
        from modules.league_config import LeagueConfig
        from unittest.mock import MagicMock, patch

        with patch.dict(os.environ, {"ATTENDANCE_SHEET_ID": "sid"}):
            cog = AttendanceCog(MagicMock(), LeagueConfig(), sheets=_client(store))
        return cog

    def test_mark_matches_ids_and_reports_unmatched(self):
        store = {
            "Points Entry S15-17": [
                ["iRacing ID", "S16R1", "S16R2"],
                ["10", "", "x"],      # finisher -> S16R1 gets 0 on sheet row 2
                ["junk", "", ""],     # unparsable id -> skipped
                ["'30", "", ""],      # quoted id -> matches 30
                ["20", "", ""],       # finisher but too-short handling: full row
            ],
        }
        cog = self._cog(store)
        result = cog._mark_attendance_in_sheet(
            "Points Entry S15-17", "iRacing ID", "S16R1", {10, 30, 44}
        )
        self.assertEqual(store["Points Entry S15-17"][1][:2], ["10", "0"])
        self.assertEqual(store["Points Entry S15-17"][3][:2], ["'30", "0"])
        self.assertEqual(store["Points Entry S15-17"][2], ["junk", "", ""])  # untouched
        self.assertEqual(store["Points Entry S15-17"][4][:2], ["20", ""])    # not a finisher
        self.assertEqual(
            result,
            {"updated": 2, "matched": 2, "unmatched": 1, "unmatched_ids": [44]},
        )

    def test_mark_unknown_target_column_raises(self):
        store = {"S": [["iRacing ID", "Other"], ["10", ""]]}
        cog = self._cog(store)
        with self.assertRaises(SheetsError):
            cog._mark_attendance_in_sheet("S", "iRacing ID", "S99R9", {10})


class RegistrationThroughSeamTests(unittest.TestCase):
    def _cog(self, store):
        from modules.registrationCog import RegistrationCog
        from modules.league_config import LeagueConfig
        from unittest.mock import MagicMock

        cog = RegistrationCog(
            MagicMock(),
            LeagueConfig(registration_sheet_name="Users"),
            sheets=_client(store),
        )
        cog.roster.spreadsheet_id = "sid"
        return cog

    def test_read_users_returns_header_zip_dicts(self):
        cog = self._cog({
            "Users": [["DiscordID", "Name"], ["7", "Zed"]]
        })
        users = cog.roster.read_users()
        self.assertEqual(users, [{"DiscordID": "7", "Name": "Zed"}])

    def test_add_then_update_round_trip(self):
        store = {"Users": [["DiscordID", "iRacingID", "CarNumber"]]}
        cog = self._cog(store)
        cog.roster.add({"DiscordID": "'123", "iRacingID": "'456", "CarNumber": "77"})
        self.assertEqual(store["Users"][1], ["123", "456", "77"])
        cog.roster.update(123, {"CarNumber": "88"})
        self.assertEqual(store["Users"][1], ["123", "456", "88"])
        with self.assertRaises(SheetsError):
            cog.roster.update(999, {"CarNumber": "1"})
        self.assertEqual(store["Users"][1], ["123", "456", "88"])


if __name__ == "__main__":
    unittest.main()
