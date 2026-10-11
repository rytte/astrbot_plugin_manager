"""Verify the network boundary before any market package is staged."""

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from astrbot_plugin_manager.market_archive import download_market_archive


def response(status=200, *, location=None, content=b"archive", length=None):
    async def chunks(size):
        yield content

    value = MagicMock(
        status=status,
        headers={"Location": location} if location else {},
        content_length=length,
        content=SimpleNamespace(iter_chunked=chunks),
    )
    value.__aenter__.return_value = value
    return value


@pytest.fixture
def http(monkeypatch):
    client = MagicMock()
    client.__aenter__.return_value = client
    monkeypatch.setattr(
        "astrbot_plugin_manager.market_archive.aiohttp.ClientSession",
        lambda **kwargs: client,
    )
    return client


@pytest.mark.parametrize(
    "destination",
    [
        "http://unsafe.example/archive.zip",
        "https://user:secret@unsafe.example/archive.zip",
    ],
)
async def test_bad_download_url_is_rejected_before_sending_request(
    http, tmp_path, destination
):
    with pytest.raises(ValueError, match="HTTPS"):
        await download_market_archive(destination, tmp_path / "download.zip")
    http.get.assert_not_called()


@pytest.mark.parametrize(
    "destination",
    [
        "http://unsafe.example/archive.zip",
        "https://user:secret@unsafe.example/archive.zip",
    ],
)
async def test_redirect_cannot_send_request_to_http_or_credentialed_target(
    http, tmp_path, destination
):
    http.get.return_value = response(302, location=destination)
    with pytest.raises(ValueError, match="HTTPS"):
        await download_market_archive(
            "https://market.example/archive.zip", tmp_path / "download.zip"
        )
    http.get.assert_called_once_with(
        "https://market.example/archive.zip", allow_redirects=False
    )
    assert not (tmp_path / "download.zip").exists()


async def test_valid_relative_redirect_downloads_package(http, tmp_path):
    http.get.side_effect = [
        response(302, location="/final.zip"),
        response(content=b"real package"),
    ]
    await download_market_archive(
        "https://market.example/archive.zip", tmp_path / "download.zip"
    )
    assert (tmp_path / "download.zip").read_bytes() == b"real package"
    assert http.get.call_args_list[-1].args[0] == "https://market.example/final.zip"


async def test_size_limit_is_enforced_on_actual_stream_without_content_length(
    http, tmp_path, monkeypatch
):
    monkeypatch.setattr("astrbot_plugin_manager.market_archive.MAX_ARCHIVE", 5)
    http.get.return_value = response(content=b"too large", length=None)
    with pytest.raises(ValueError, match="256 MiB"):
        await download_market_archive(
            "https://market.example/archive.zip", tmp_path / "download.zip"
        )
    assert (tmp_path / "download.zip").stat().st_size == 0
