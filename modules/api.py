"""
iRacing API Handler for accessing the iRacing API.

This module provides classes and functions to interact with the iRacing API,
authenticate, retrieve session data, and validate sessions against expectations.
"""

import base64
import hashlib
from time import time
from typing import Any, Optional, cast

import requests


class iRacingAPIHandler(requests.Session):
    """
    Handler for interacting with the iRacing API.

    This class extends requests.Session to manage authentication and
    provide methods for retrieving and validating session data.

    Supports two authentication flows:
    - Legacy authentication (default, for backwards compatibility)
    - OAuth 2.1 Password Limited Flow (for headless/Docker environments)
    """

    # OAuth endpoints
    OAUTH_TOKEN_URL = "https://oauth.iracing.com/oauth2/token"

    def __init__(
        self,
        email: str,
        password: str,
        client_id: Optional[str] = None,
        client_secret: Optional[str] = None,
        use_oauth: bool = False,
    ):
        """
        Initialize the API handler.

        Args:
            email: iRacing account email
            password: iRacing account password
            client_id: OAuth client ID (required if use_oauth=True)
            client_secret: OAuth client secret (required if use_oauth=True)
            use_oauth: If True, use OAuth 2.1 Password Limited Flow; if False, use legacy auth
        """
        super().__init__()

        self.email: str = email
        self.raw_password: str = password
        self.client_id: Optional[str] = client_id
        self.client_secret: Optional[str] = client_secret
        self.use_oauth: bool = use_oauth

        # Legacy auth password (hashed)
        self.password: str = str(
            base64.b64encode(
                hashlib.sha256(f"{password}{str(email).lower()}".encode()).digest()
            )
        )
        # remove b' and ' from the ends of the string
        self.password = self.password[2:-1]

        # OAuth token management
        self.access_token: Optional[str] = None
        self.refresh_token: Optional[str] = None
        self.token_expires_at: float = 0
        self.refresh_token_expires_at: float = 0

        self.logged_in: bool = False
        _ = self.login()

    def _mask_secret(self, secret: str, identifier: str) -> str:
        """
        Mask a secret (client_secret or password) using iRacing's masking algorithm.

        Args:
            secret: The secret to mask
            identifier: client_id for client_secret, username for password

        Returns:
            Base64 encoded SHA-256 hash of secret + normalized_identifier
        """
        # Normalize the identifier (trim and lowercase)
        normalized_id = identifier.strip().lower()

        # Concatenate secret with normalized identifier
        combined = f"{secret}{normalized_id}"

        # Hash with SHA-256 and encode with base64
        hasher = hashlib.sha256()
        hasher.update(combined.encode("utf-8"))

        return base64.b64encode(hasher.digest()).decode("utf-8")

    def _login_password_limited_flow(self) -> bool:
        """
        Authenticate using OAuth 2.1 Password Limited Flow.

        Returns:
            True if login is successful

        Raises:
            Exception: If authentication fails
        """
        if not self.client_id or not self.client_secret:
            raise Exception(
                "client_id and client_secret are required for OAuth Password Limited Flow"
            )

        if not self.email or not self.raw_password:
            raise Exception(
                "email and password are required for OAuth Password Limited Flow"
            )

        # Mask the client secret and password
        masked_secret = self._mask_secret(self.client_secret, self.client_id)
        masked_password = self._mask_secret(self.raw_password, self.email)

        # Request tokens
        token_data = {
            "grant_type": "password_limited",
            "client_id": self.client_id,
            "client_secret": masked_secret,
            "username": self.email,
            "password": masked_password,
            "scope": "iracing.auth",
        }

        response = requests.post(
            self.OAUTH_TOKEN_URL,
            data=token_data,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )

        if response.status_code != 200:
            raise Exception(
                f"OAuth Password Limited authentication failed: {response.text}"
            )

        return self._process_token_response(response.json())

    def _process_token_response(self, token_response: dict[str, Any]) -> bool:
        """
        Process token response and store tokens.

        Args:
            token_response: Response from token endpoint

        Returns:
            True if successful
        """
        self.access_token = token_response.get("access_token")
        self.refresh_token = token_response.get("refresh_token")

        # Calculate expiry times
        expires_in = token_response.get("expires_in", 600)
        self.token_expires_at = time() + expires_in

        refresh_expires_in = token_response.get("refresh_token_expires_in", 604800)
        self.refresh_token_expires_at = time() + refresh_expires_in

        self.logged_in = True
        return True

    def _refresh_access_token(self) -> bool:
        """
        Refresh the access token using the refresh token.

        Returns:
            True if refresh is successful

        Raises:
            Exception: If refresh fails
        """
        if not self.refresh_token:
            raise Exception("No refresh token available")

        if time() >= self.refresh_token_expires_at:
            raise Exception("Refresh token has expired")

        token_data = {
            "grant_type": "refresh_token",
            "client_id": self.client_id,
            "refresh_token": self.refresh_token,
        }

        if self.client_secret and self.client_id:
            token_data["client_secret"] = self._mask_secret(
                self.client_secret, self.client_id
            )

        response = requests.post(
            self.OAUTH_TOKEN_URL,
            data=token_data,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )

        if response.status_code != 200:
            raise Exception(f"Failed to refresh token: {response.text}")

        return self._process_token_response(response.json())

    def _login_legacy(self) -> bool:
        """
        Log in to the iRacing API using legacy authentication.

        Returns:
            True if login is successful

        Raises:
            Exception: If authentication fails
        """
        url = "https://members-ng.iracing.com/auth"
        login_headers = {"Content-Type": "application/json"}
        data = {"email": self.email, "password": self.password}

        response = self.post(url, json=data, headers=login_headers)
        response_data = cast(
            dict[str, Any],
            response.json(),  # pyright: ignore[reportExplicitAny]
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

    def login(self) -> bool:
        return self._login_password_limited_flow()

    def request(
        self, method: str, url: str, *args: Any, **kwargs: Any
    ) -> requests.Response:
        """
        Override request method to add Bearer token authentication for OAuth.

        Args:
            method: HTTP method
            url: URL to request
            *args: Additional arguments
            **kwargs: Additional keyword arguments

        Returns:
            Response object
        """
        # Check if using OAuth and token needs refresh (with 30 second buffer)
        if self.use_oauth and self.logged_in and time() >= (self.token_expires_at - 30):
            try:
                self._refresh_access_token()
            except Exception:
                # If refresh fails, try to re-authenticate
                self.logged_in = False
                self.login()

        # Add Bearer token to headers for OAuth authentication
        # Only for iRacing API URLs (not S3 presigned URLs)
        if (
            self.use_oauth
            and self.access_token
            and ("members-ng.iracing.com" in url or "oauth.iracing.com" in url)
        ):
            if "headers" not in kwargs:
                kwargs["headers"] = {}
            kwargs["headers"]["Authorization"] = f"Bearer {self.access_token}"

        return super().request(method, url, *args, **kwargs)

    def _get_paged_data(self, url: str) -> dict[str, Any]:  # pyright: ignore[reportExplicitAny]
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
