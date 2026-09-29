import os
from typing import Any

import requests
from dotenv import load_dotenv


class DhanClient:
    """
    Basic DhanHQ REST API client.

    This class is intentionally limited to read-only operations
    during the initial development phase.
    """

    BASE_URL = "https://api.dhan.co/v2"

    def __init__(self) -> None:
        load_dotenv()

        self.client_id = os.getenv("DHAN_CLIENT_ID")
        self.access_token = os.getenv("DHAN_ACCESS_TOKEN")

        if not self.client_id:
            raise RuntimeError(
                "DHAN_CLIENT_ID is missing from environment."
            )

        if not self.access_token:
            raise RuntimeError(
                "DHAN_ACCESS_TOKEN is missing from environment."
            )

        self.session = requests.Session()

        self.session.headers.update({
            "access-token": self.access_token,
            "Content-Type": "application/json",
        })

    def get_profile(self) -> dict[str, Any]:
        """
        Retrieve the authenticated Dhan account profile.
        """

        response = self.session.get(
            f"{self.BASE_URL}/profile",
            timeout=10,
        )

        response.raise_for_status()

        return response.json()