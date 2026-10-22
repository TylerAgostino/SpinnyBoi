# pyright: basic
"""SheetsClient: the single seam over the Google Sheets API.

Both cogs that touch spreadsheets (Registration, Attendance) go through
this module; nobody else calls googleapiclient. The interface speaks in
records and columns, not request objects:

    read_rows        sheet tab -> list of {header: value} dicts
    read_grid        sheet tab -> (headers, raw data rows)
    append_record    append one {header: value} dict, header-ordered
    update_record    merge updates into the row whose key_column matches
    update_column_cells  many single-cell writes in one batchUpdate

Credential loading (GOOGLE_CREDENTIALS_FILE, else GOOGLE_CREDENTIALS_JSON)
is the module's job: a missing config yields an unavailable client whose
methods raise SheetsError, never a crash at import or construction.

Blocking: all methods are synchronous (googleapiclient is); callers in
async code use asyncio.to_thread, as the cogs already do.
"""
from __future__ import annotations

import json
import logging
import os
import re
from typing import Any, Dict, List, Optional, Tuple

from modules.env_config import env_str

try:
    from google.oauth2.service_account import Credentials
    from googleapiclient.discovery import build
    from googleapiclient.errors import HttpError

    GOOGLE_APIS_AVAILABLE = True
except ImportError:
    logging.error(
        "Google API libraries not installed. Sheets features will be limited."
    )
    GOOGLE_APIS_AVAILABLE = False

SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]

_LEADING_QUOTE = re.compile(r"^'")


class SheetsError(Exception):
    """Any Sheets failure: unavailable client, API error, bad sheet/column."""


def strip_id_quote(value: Any) -> str:
    """Drop the leading apostrophe used to force text cells (e.g. \"'12345\")."""
    return _LEADING_QUOTE.sub("", str(value).strip())


def _col_index_to_letter(n: int) -> str:
    """Convert a 0-based column index to an Excel-style column letter (A, B, ..., Z, AA, ...)."""
    result = ""
    while n >= 0:
        result = chr(n % 26 + ord("A")) + result
        n = n // 26 - 1
    return result


def credentials_from_env() -> Optional[Any]:
    """Service-account credentials from GOOGLE_CREDENTIALS_FILE, else GOOGLE_CREDENTIALS_JSON."""
    if not GOOGLE_APIS_AVAILABLE:
        return None
    try:
        creds_file = env_str(os.environ, "GOOGLE_CREDENTIALS_FILE", "credentials.json")
        if os.path.exists(creds_file):
            logging.info("Google Sheets credentials loaded from file")
            return Credentials.from_service_account_file(creds_file, scopes=SCOPES)
        creds_json_str = os.getenv("GOOGLE_CREDENTIALS_JSON")
        if creds_json_str:
            logging.info("Google Sheets credentials loaded from environment")
            return Credentials.from_service_account_info(
                json.loads(creds_json_str), scopes=SCOPES
            )
        logging.error("No Google Sheets credentials found")
    except Exception as ex:
        logging.error(f"Error initializing Google Sheets API: {str(ex)}")
    return None


class SheetsClient:
    """Deep module over one Google Sheets connection. Unavailable != broken:
    construct always succeeds; first use of a credential-less client raises
    SheetsError."""

    def __init__(self, credentials: Optional[Any] = None):
        self._credentials = credentials
        self._service: Optional[Any] = None

    @classmethod
    def from_env(cls) -> "SheetsClient":
        return cls(credentials_from_env())

    @property
    def available(self) -> bool:
        return self._credentials is not None

    # -- reads ---------------------------------------------------------

    def read_grid(
        self, spreadsheet_id: str, sheet_name: str
    ) -> Tuple[List[str], List[List[str]]]:
        """Return (headers, data_rows) for the tab. SheetsError on failure."""
        values = self._get_values(spreadsheet_id, f"'{sheet_name}'!A1:ZZ")
        if not values:
            raise SheetsError(f"Sheet '{sheet_name}' appears to be empty or does not exist.")
        return values[0], values[1:]

    def read_rows(
        self, spreadsheet_id: str, sheet_name: str
    ) -> List[Dict[str, str]]:
        """Return the tab as {header: value} dicts; short rows padded with ''."""
        try:
            headers, data_rows = self.read_grid(spreadsheet_id, sheet_name)
        except SheetsError:
            return []
        return [_zip_row(headers, row) for row in data_rows]

    # -- writes --------------------------------------------------------

    def append_record(
        self, spreadsheet_id: str, sheet_name: str, record: Dict[str, str]
    ) -> None:
        """Append one record below the existing data, ordered by the tab's headers."""
        headers, _ = self.read_grid(spreadsheet_id, sheet_name)
        row = [record.get(header, "") for header in headers]
        service = self._get_service()
        try:
            service.spreadsheets().values().append(
                spreadsheetId=spreadsheet_id,
                range=f"'{sheet_name}'!A:Z",
                valueInputOption="USER_ENTERED",
                insertDataOption="INSERT_ROWS",
                body={"values": [row]},
            ).execute()
        except HttpError as err:
            raise SheetsError(f"Google Sheets API error while appending: {err}") from err

    def update_record(
        self,
        spreadsheet_id: str,
        sheet_name: str,
        key_column: str,
        key_value: Any,
        updates: Dict[str, str],
    ) -> None:
        """Merge `updates` into the row whose key_column matches key_value
        (quote- and type-insensitive). Rewrites the full row; cells absent
        from `updates` keep their current value. SheetsError if the key
        column or row is missing."""
        headers, data_rows = self.read_grid(spreadsheet_id, sheet_name)
        if key_column not in headers:
            raise SheetsError(f"Column '{key_column}' not found in sheet '{sheet_name}'.")
        key_idx = headers.index(key_column)
        row_num = _find_row(data_rows, key_idx, key_value)
        if row_num is None:
            raise SheetsError(
                f"No row in '{sheet_name}' with {key_column}={key_value}."
            )
        current = _zip_row(headers, data_rows[row_num])
        merged = {**current, **updates}
        row = [merged.get(header, "") for header in headers]
        service = self._get_service()
        try:
            service.spreadsheets().values().update(
                spreadsheetId=spreadsheet_id,
                range=f"'{sheet_name}'!A{row_num + 2}",
                valueInputOption="USER_ENTERED",
                body={"values": [row]},
            ).execute()
        except HttpError as err:
            raise SheetsError(f"Google Sheets API error while updating: {err}") from err

    def update_column_cells(
        self,
        spreadsheet_id: str,
        sheet_name: str,
        column_header: str,
        cell_values: Dict[int, Any],
    ) -> int:
        """Write values into single cells of `column_header`, keyed by sheet
        row number (1-based, header row = 1). One batchUpdate; returns the
        number of cells written."""
        if not cell_values:
            return 0
        headers, _ = self.read_grid(spreadsheet_id, sheet_name)
        if column_header not in headers:
            raise SheetsError(
                f"Column '{column_header}' was not found in sheet headers. "
                f"Available headers: {headers}"
            )
        letter = _col_index_to_letter(headers.index(column_header))
        data = [
            {"range": f"'{sheet_name}'!{letter}{row_num}", "values": [[value]]}
            for row_num, value in sorted(cell_values.items())
        ]
        service = self._get_service()
        try:
            service.spreadsheets().values().batchUpdate(
                spreadsheetId=spreadsheet_id,
                body={"valueInputOption": "USER_ENTERED", "data": data},
            ).execute()
        except HttpError as err:
            raise SheetsError(
                f"Google Sheets API error while writing updates: {err}"
            ) from err
        return len(data)

    # -- google plumbing (private) --------------------------------------

    def _get_service(self):
        if not self.available:
            raise SheetsError(
                "Google Sheets service is not available. Check credentials."
            )
        if self._service is None:
            try:
                self._service = build("sheets", "v4", credentials=self._credentials)
            except Exception as ex:
                raise SheetsError(f"Error building Google Sheets service: {ex}") from ex
        return self._service

    def _get_values(self, spreadsheet_id: str, range_name: str) -> List[List[str]]:
        service = self._get_service()
        try:
            result = (
                service.spreadsheets()
                .values()
                .get(spreadsheetId=spreadsheet_id, range=range_name)
                .execute()
            )
        except HttpError as err:
            raise SheetsError(f"Google Sheets API error while reading sheet: {err}") from err
        return result.get("values") or []


def _zip_row(headers: List[str], row: List[str]) -> Dict[str, str]:
    padded = list(row) + [""] * (len(headers) - len(row))
    return {header: padded[i] for i, header in enumerate(headers)}


def _find_row(
    data_rows: List[List[str]], key_idx: int, key_value: Any
) -> Optional[int]:
    target = strip_id_quote(key_value)
    for i, row in enumerate(data_rows):
        if len(row) > key_idx and strip_id_quote(row[key_idx]) == target:
            return i
    return None
