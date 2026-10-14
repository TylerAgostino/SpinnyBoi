# pyright: basic
"""RegistrationRoster: the league roster of registered drivers.

Owns everything about the roster itself — reading rows, matching a Discord
id to a row, adding/updating rows, the registration business rules (id and
car-number validation, iRacing name lookup, taken-number bookkeeping) and
the registration-vs-league comparison (`registration_status`). The
Registration cog keeps only Discord concerns: modals, embeds, the driver
role, and the league nickname.

`registration_status` and `penalty_prefix` are pure functions: the sweep's
rules are testable without Discord, Sheets, or the network.

The roster writes force-text ids with a leading apostrophe (e.g. "'12345")
so Sheets never renders long ids in scientific notation; reads may see the
apostrophe stripped by the server, so matching strips it on both sides.
"""
from __future__ import annotations
import datetime
import re
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Tuple

from .sheets_client import SheetsClient, SheetsError, strip_id_quote

_USERS_CACHE_TTL_UNUSED = None  # placeholder removed; cache lives in the cog


def penalty_prefix(points: Any) -> str:
    """The iRacing league prefix for a penalty-point total: '', (X)...(XXXX)."""
    try:
        pts = int(points)
    except (TypeError, ValueError):
        return ""
    if pts < 3:
        return ""
    elif pts <= 5:
        return "(X)"
    elif pts <= 8:
        return "(XX)"
    elif pts <= 11:
        return "(XXX)"
    else:
        return "(XXXX)"


@dataclass
class RegistrationStatus:
    """Outcome of comparing registered drivers against the league roster."""

    name_changes: List[Tuple[str, str]] = field(default_factory=list)
    number_changes: List[Tuple[str, str, str]] = field(default_factory=list)
    invites: List[str] = field(default_factory=list)
    accept_invites: List[str] = field(default_factory=list)
    penalty_prefix_changes: List[Tuple[str, str, str]] = field(default_factory=list)


def registration_status(
    registered_drivers: List[Dict[str, Any]],
    league_members: List[Dict[str, Any]],
    pending_invites: List[Dict[str, Any]],
) -> RegistrationStatus:
    """Compare each registered driver's sheet row to their iRacing league
    membership. Pure: given the three lists it decides every action."""
    status = RegistrationStatus()
    for driver in registered_drivers:
        if not driver.get("RegistrationDate"):
            continue  # skip unregistered drivers
        iracing_id = strip_id_quote(driver.get("iRacingID", ""))
        desired_name = driver.get("DesiredName", "")
        desired_number = driver.get("CarNumber", "")
        prefix = penalty_prefix(driver.get("PenaltyPoints", ""))
        expected_name = f"{prefix} {desired_name}" if prefix else desired_name

        member = next(
            (m for m in league_members if str(m.get("cust_id")) == iracing_id), None
        )
        pending_member = next(
            (m for m in pending_invites if str(m.get("cust_id")) == iracing_id), None
        )

        if not iracing_id:
            continue
        if not member and not pending_member:
            status.invites.append(desired_name)
        elif pending_member:
            status.accept_invites.append(desired_name)
        else:
            current_name = member.get("nick_name") or member.get("display_name", "")
            # Strip any existing (X) prefix to get the bare current name
            current_name_stripped = re.sub(r"^\([xX]{1,4}\)\s*", "", current_name)
            current_number = member.get("car_number", "")

            if current_name_stripped != desired_name:
                # The base name itself is wrong — real name change needed
                status.name_changes.append((current_name_stripped, desired_name))
            elif current_name != expected_name:
                # Base name is fine but the penalty prefix is wrong
                status.penalty_prefix_changes.append(
                    (desired_name, current_name, expected_name)
                )

            if current_number != desired_number:
                status.number_changes.append(
                    (current_name_stripped, desired_number, current_number)
                )
    return status


class RegistrationRoster:
    """The registration sheet as roster domain. Sync: callers in async code
    use asyncio.to_thread."""

    def __init__(
        self, sheets: SheetsClient, spreadsheet_id: Optional[str], sheet_name: str
    ):
        self.sheets = sheets
        self.spreadsheet_id = spreadsheet_id
        self.sheet_name = sheet_name

    def read_users(self) -> List[Dict[str, Any]]:
        if not self.spreadsheet_id:
            return []
        return self.sheets.read_rows(self.spreadsheet_id, self.sheet_name)

    def add(self, user_data: Dict[str, Any]) -> None:
        if not self.spreadsheet_id:
            raise SheetsError("REGISTRATION_SHEET_ID is not configured.")
        self.sheets.append_record(self.spreadsheet_id, self.sheet_name, user_data)

    def update(self, discord_id: int, user_data: Dict[str, Any]) -> None:
        if not self.spreadsheet_id:
            raise SheetsError("REGISTRATION_SHEET_ID is not configured.")
        self.sheets.update_record(
            self.spreadsheet_id, self.sheet_name, "DiscordID", discord_id, user_data
        )

    def register_driver(
        self,
        discord_id: int,
        iracing_id: Any,
        desired_league_name: str,
        desired_league_number: str,
        num_races: str,
        driver_name: Callable[[int], Optional[str]],
    ) -> Dict[str, Any]:
        """Run the full registration rules and write the row.

        Raises ValueError with a user-facing message on any rule failure
        (invalid ids, unknown iRacing customer, taken car number). Returns
        the written user_data on success. SheetsError propagates.
        """
        members = self.read_users()
        # Match against freshly-read rows, not any cache, so the
        # add-vs-update decision stays correct after concurrent edits.
        existing_user = match_user(members, discord_id)
        other_members = [
            m for m in members if strip_id_quote(m.get("DiscordID", "")) != str(discord_id)
        ]
        user_data = {
            "DiscordID": f"'{discord_id}",
            "iRacingID": f"'{iracing_id}",
            "DesiredName": desired_league_name,
            "NumRaces": num_races,
        }

        if not re.fullmatch(r"\d{1,8}", str(iracing_id)):
            raise ValueError("iRacing ID not valid.")
        if not re.fullmatch(r"\d{1,3}", desired_league_number):
            raise ValueError("Car Number not valid.")

        iracing_name = driver_name(int(iracing_id))
        if iracing_name is None:
            raise ValueError(
                "iRacing ID not found. Please double-check your Customer ID and try again."
            )
        user_data["iRacingName"] = iracing_name

        if desired_league_number in [m.get("CarNumber", "") for m in other_members]:
            if existing_user:
                user_data["DesiredButTakenCarNums"] = (
                    f"'{desired_league_number}, {existing_user.get('DesiredButTakenCarNums', '')}".strip(
                        ", "
                    )
                )
                self.update(discord_id, user_data)
            else:
                user_data["DesiredButTakenCarNums"] = f"'{desired_league_number}"
                self.add(user_data)
            raise ValueError(
                f"Desired league number {desired_league_number} is already taken."
            )
        user_data["CarNumber"] = f"'{desired_league_number}"
        user_data["RegistrationDate"] = datetime.datetime.utcnow().strftime(
            "%Y-%m-%d %H:%M:%S"
        )

        if existing_user:
            self.update(discord_id, user_data)
        else:
            self.add(user_data)
        return user_data


def match_user(
    users: List[Dict[str, Any]], discord_id: int
) -> Optional[Dict[str, Any]]:
    for user in users:
        if strip_id_quote(user.get("DiscordID", "")) == str(discord_id):
            return user
    return None
