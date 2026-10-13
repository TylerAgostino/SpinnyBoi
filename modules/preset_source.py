# pyright: basic
"""PresetSource: the presets spreadsheet as a deep module.

Owns the presets sheet (preset name -> tab -> filter string) and answers
the questions the wheel commands ask: which presets exist (autocomplete),
which tabs a preset has, what filter a (preset, tab) pair uses, and the
raw frame of any option tab (``read_tab``).

A single shared instance via ``default()`` serves both the WheelCog
commands and the module-level autocomplete callbacks, so nothing needs a
cog global. The Google-Sheets-via-csv-URL is the only adapter; tests
inject a fake ``csv_url``.

Contract:
- Lookups are case-insensitive on preset Fullname and tab names.
- ``preset()`` / ``filter_for()`` raise ``PresetNotFound`` when there is
  no matching row/column; ``tabs()`` returns ``[]`` instead.
- ``refresh()`` swaps the cached frame only on success; on failure it
  logs and keeps serving the previous frame. ``load()`` is strict.
"""
import asyncio
import logging
import os
from typing import Callable, List, Optional, Tuple

import pandas as pd

log = logging.getLogger(__name__)


class PresetNotFound(Exception):
    """No row (or column) in the presets sheet matches that name."""


class Preset:
    """One preset row: its filter columns, in sheet order."""

    def __init__(self, name: str, record):
        self.name = name
        self._record = record

    def _tab_names(self) -> List[str]:
        return [
            k
            for k, v in self._record.items()
            if isinstance(v, str) and k != "Fullname"
        ]

    def tabs(self) -> List[str]:
        return self._tab_names()

    def filters(self) -> List[Tuple[str, str]]:
        """(tab, filter_string) pairs for every filter column."""
        return [(tab, str(self._record[tab])) for tab in self._tab_names()]

    def resolve_tab(self, tab_name: str) -> Tuple[str, str]:
        """(actual tab name, filter string); tab matched case-insensitively.

        The actual sheet tab name is returned so callers can fetch the
        option sheet Google serves under its real name."""
        for tab in self._tab_names():
            if tab.lower() == tab_name.lower():
                return tab, str(self._record[tab])
        raise PresetNotFound(tab_name)


class PresetSource:
    def __init__(self, csv_url: Callable[[str], str]):
        """``csv_url(sheet_name)`` returns a URL pandas can ``read_csv``."""
        self._csv_url = csv_url
        self._frame: Optional[pd.DataFrame] = None

    @classmethod
    def from_env(cls) -> "PresetSource":
        base_url = f'https://docs.google.com/spreadsheets/d/{os.getenv("GSHEET_ID")}'
        return cls(
            lambda sheet: f"{base_url}/gviz/tq?tqx=out:csv&sheet={sheet}"
        )

    def load(self) -> pd.DataFrame:
        """Fetch the presets frame (sync); raises if the fetch fails."""
        self._frame = pd.read_csv(self._csv_url("presets"))
        return self._frame

    async def refresh(self) -> bool:
        """Reload off the event loop; the old frame survives failures."""
        try:
            await asyncio.to_thread(self.load)
            return True
        except Exception as e:
            log.error(f"Error refreshing presets dataframe: {str(e)}")
            return False

    async def read_tab(self, sheet: str) -> pd.DataFrame:
        """Raw frame for any option tab, fetched off the event loop."""
        return await asyncio.to_thread(pd.read_csv, self._csv_url(sheet))

    def _loaded(self) -> pd.DataFrame:
        if self._frame is None:
            self.load()
        assert self._frame is not None
        return self._frame

    def _row(self, preset_name: str):
        frame = self._loaded()
        names = frame["Fullname"].astype("string").str.lower()
        matches = frame[names == (preset_name or "").lower()]
        if matches.empty:
            raise PresetNotFound(preset_name)
        return matches.to_dict("records")[0]

    def names(self) -> List[str]:
        return [x["Fullname"] for x in self._loaded().to_dict("records")]

    def tabs(self, preset_name: str) -> List[str]:
        try:
            return Preset(preset_name, self._row(preset_name)).tabs()
        except PresetNotFound:
            log.error(f"Error getting tabs for preset {preset_name}: not found")
            return []

    def preset(self, preset_name: str) -> Preset:
        return Preset(preset_name, self._row(preset_name))

    @classmethod
    def default(cls) -> "PresetSource":
        """The shared PresetSource, built from the environment on first use."""
        global _DEFAULT
        if _DEFAULT is None:
            _DEFAULT = cls.from_env()
        return _DEFAULT


_DEFAULT: Optional["PresetSource"] = None
