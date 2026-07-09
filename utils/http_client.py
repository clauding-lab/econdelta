"""Shared HTTP session with retries, timeout, and User-Agent."""

import logging
from typing import Any

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from utils.ca_bundle import combined_ca_bundle

logger = logging.getLogger(__name__)

_USER_AGENT = "econdelta/0.1 (+https://github.com/clauding-lab/econdelta)"
_DEFAULT_TIMEOUT = 30
_RETRY_TOTAL = 3
_RETRY_BACKOFF_FACTOR = 2  # sleeps: 2s, 4s, 8s after 1st/2nd/3rd failure
_RETRY_STATUS_FORCELIST = [429, 500, 502, 503, 504]


class HttpClient:
    """Requests session with retry logic and a fixed User-Agent.

    Usage:
        client = HttpClient()
        html = client.fetch_html("https://example.com")
        data = client.fetch_json("https://api.example.com/endpoint")
    """

    class FetchError(Exception):
        """Raised when a request fails after all retries or returns non-200."""

        def __init__(self, url: str, status_code: int | None, message: str) -> None:
            self.url = url
            self.status_code = status_code
            super().__init__(f"FetchError [{status_code}] {url}: {message}")

    def __init__(
        self,
        timeout: int = _DEFAULT_TIMEOUT,
        retry_total: int = _RETRY_TOTAL,
        backoff_factor: float = _RETRY_BACKOFF_FACTOR,
    ) -> None:
        self._timeout = timeout
        self._session = requests.Session()
        self._session.headers.update({"User-Agent": _USER_AGENT})
        # Verify against certifi PLUS the vendored intermediates (fetchers/ca/*.pem,
        # shared with fetchers/tls.py — one cert file, one rotation point). DSE's
        # servers send an incomplete chain (leaf only, missing the Sectigo R36
        # intermediate); this additive bundle lets requests verify them without
        # ever disabling verification. Falls back to certifi on any build failure.
        self._session.verify = combined_ca_bundle()

        retry = Retry(
            total=retry_total,
            backoff_factor=backoff_factor,
            status_forcelist=_RETRY_STATUS_FORCELIST,
            allowed_methods=["GET", "HEAD"],
            raise_on_status=False,
        )
        adapter = HTTPAdapter(max_retries=retry)
        self._session.mount("https://", adapter)
        self._session.mount("http://", adapter)

    def get(self, url: str, **kwargs: Any) -> requests.Response:
        """Perform a GET request.

        Args:
            url: Target URL.
            **kwargs: Passed through to requests.Session.get. 'timeout' may be
                      overridden per-call; defaults to the instance timeout.

        Returns:
            requests.Response

        Raises:
            HttpClient.FetchError: On connection error or after all retries exhausted.
        """
        kwargs.setdefault("timeout", self._timeout)
        try:
            response = self._session.get(url, **kwargs)
        except requests.exceptions.RequestException as exc:
            raise HttpClient.FetchError(url, None, str(exc)) from exc
        return response

    def fetch_html(self, url: str, **kwargs: Any) -> str:
        """Fetch a URL and return the response body as text.

        Args:
            url: Target URL.
            **kwargs: Forwarded to self.get().

        Returns:
            Response body text.

        Raises:
            HttpClient.FetchError: On non-200 status or network failure.
        """
        response = self.get(url, **kwargs)
        if response.status_code != 200:
            raise HttpClient.FetchError(
                url,
                response.status_code,
                f"Non-200 response: {response.status_code}",
            )
        return response.text

    def fetch_json(self, url: str, **kwargs: Any) -> dict:
        """Fetch a URL and return the response body parsed as JSON.

        Args:
            url: Target URL.
            **kwargs: Forwarded to self.get().

        Returns:
            Parsed JSON as a dict.

        Raises:
            HttpClient.FetchError: On non-200 status or network failure.
            requests.exceptions.JSONDecodeError: If response body is not valid JSON.
        """
        response = self.get(url, **kwargs)
        if response.status_code != 200:
            raise HttpClient.FetchError(
                url,
                response.status_code,
                f"Non-200 response: {response.status_code}",
            )
        return response.json()


# Module-level singleton — import and use directly in scrapers
DEFAULT_CLIENT = HttpClient()
