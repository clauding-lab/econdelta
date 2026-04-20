"""Tests for utils/http_client.py."""

from unittest.mock import MagicMock, patch

import pytest
import requests

from utils.http_client import DEFAULT_CLIENT, HttpClient


class TestDefaultClientSingleton:
    def test_default_client_exists(self):
        assert DEFAULT_CLIENT is not None

    def test_default_client_is_http_client(self):
        assert isinstance(DEFAULT_CLIENT, HttpClient)


class TestHttpClientInit:
    def test_session_has_user_agent(self):
        client = HttpClient()
        ua = client._session.headers.get("User-Agent", "")
        assert "econdelta" in ua

    def test_custom_timeout_stored(self):
        client = HttpClient(timeout=60)
        assert client._timeout == 60


class TestFetchHtml:
    def test_fetch_html_returns_text_on_200(self):
        client = HttpClient()
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.text = "<html>hello</html>"

        with patch.object(client._session, "get", return_value=mock_response) as mock_get:
            result = client.fetch_html("https://example.com")

        assert result == "<html>hello</html>"
        mock_get.assert_called_once()

    def test_fetch_html_raises_fetch_error_on_404(self):
        client = HttpClient()
        mock_response = MagicMock()
        mock_response.status_code = 404

        with patch.object(client._session, "get", return_value=mock_response):
            with pytest.raises(HttpClient.FetchError) as exc_info:
                client.fetch_html("https://example.com/missing")

        assert exc_info.value.status_code == 404
        assert "example.com" in str(exc_info.value)

    def test_fetch_html_raises_fetch_error_on_connection_error(self):
        client = HttpClient()

        with patch.object(
            client._session,
            "get",
            side_effect=requests.exceptions.ConnectionError("refused"),
        ):
            with pytest.raises(HttpClient.FetchError) as exc_info:
                client.fetch_html("https://unreachable.invalid/")

        assert exc_info.value.status_code is None


class TestFetchJson:
    def test_fetch_json_returns_dict_on_200(self):
        client = HttpClient()
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"key": "value"}

        with patch.object(client._session, "get", return_value=mock_response):
            result = client.fetch_json("https://api.example.com/data")

        assert result == {"key": "value"}

    def test_fetch_json_raises_on_non_200(self):
        client = HttpClient()
        mock_response = MagicMock()
        mock_response.status_code = 500

        with patch.object(client._session, "get", return_value=mock_response):
            with pytest.raises(HttpClient.FetchError) as exc_info:
                client.fetch_json("https://api.example.com/error")

        assert exc_info.value.status_code == 500


class TestFetchErrorAttributes:
    def test_fetch_error_carries_url_and_status(self):
        err = HttpClient.FetchError("https://x.com", 503, "Service Unavailable")
        assert err.url == "https://x.com"
        assert err.status_code == 503
        assert "503" in str(err)
