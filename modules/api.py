"""
iRacing API Handler for accessing the iRacing API.

This module provides classes and functions to interact with the iRacing API,
authenticate, retrieve session data, and validate sessions against expectations.
"""

import hashlib
import requests
import base64
from typing import Any, cast


class iRacingAPIHandler(requests.Session):
    """
    Handler for interacting with the iRacing API.

    This class extends requests.Session to manage authentication and
    provide methods for retrieving and validating session data.
    """

    # Constants already imported from validation module

    def __init__(self, email: str, password: str):
        """
        Initialize the API handler.

        Args:
            email: iRacing account email
            password: iRacing account password
        """
        self.email: str = email
        self.password: str = str(
            base64.b64encode(
                hashlib.sha256(f"{password}{str(email).lower()}".encode()).digest()
            )
        )
        # remove b' and ' from the ends of the string
        self.password = self.password[2:-1]
        self.logged_in: bool = False
        super().__init__()
        _ = self.login()

    def login(self) -> bool:
        """
        Log in to the iRacing API.

        Returns:
            True if login is successful, False otherwise

        Raises:
            VerificationRequiredException: If verification is required
            UnauthorizedException: If authentication fails
        """
        url = "https://members-ng.iracing.com/auth"
        login_headers = {"Content-Type": "application/json"}
        data = {"email": self.email, "password": self.password}

        response = self.post(url, json=data, headers=login_headers)
        response_data = cast(
            dict[str, Any], response.json()  # pyright: ignore[reportExplicitAny]
        )

        if response.status_code == 200 and response_data.get("authcode"):
            # save the returned cookie
            if response.cookies:
                self.cookies.update(response.cookies)
            self.logged_in = True
            return True
        elif (
            "verificationRequired" in response.json()
            and response.json()["verificationRequired"]
        ):
            raise Exception(
                f"Please log in to the iRacing member site. {response_data}"
            )
        else:
            raise Exception(f"Error from iRacing: {response_data}")

    def _get_paged_data(
        self, url: str
    ) -> dict[str, Any]:  # pyright: ignore[reportExplicitAny]
        """
        Get paginated data from the API.

        Args:
            url: URL to fetch data from

        Returns:
            dictionary containing the fetched data
        """
        if not self.logged_in:
            _ = self.login()
            if not self.logged_in:
                raise Exception("Not logged in to iRacing API")
        response = self.get(url)
        if response.status_code == 200:
            if "link" in response.json():
                data = self.get(response.json()["link"])
                return data.json() if data.status_code == 200 else {}
            else:
                return cast(
                    dict[str, Any],  # pyright: ignore[reportExplicitAny]
                    response.json(),
                )
        elif response.status_code == 401:
            self.logged_in = False
            return self._get_paged_data(url)
        else:
            response.raise_for_status()
            return {}

    def get_league_members(self, league_id: int, pending=False) -> list[dict[str, Any]]:
        """
        Get members of a league.

        Args:
            league_id: ID of the league

        Returns:
            dictionary containing league members
        """
        url = f"https://members-ng.iracing.com/data/league/get?league_id={league_id}"
        response = self._get_paged_data(url)
        if pending:
            return response.get("pending_requests", [])
        else:
            return response.get("roster", [])
