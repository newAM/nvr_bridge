"""Tests for the NVR request-line tolerance (device_http)."""

from types import SimpleNamespace

import pytest

gunicorn = pytest.importorskip("gunicorn")

from gunicorn.http.errors import InvalidRequestLine
from nvr_gateway.device_http import DeviceRequest


def make_request() -> DeviceRequest:
    request = DeviceRequest.__new__(DeviceRequest)  # skip Request.__init__
    request.cfg = SimpleNamespace(
        permit_unconventional_http_method=False, casefold_http_method=False
    )
    return request


def test_missing_leading_slash_normalized() -> None:
    request = make_request()
    request.parse_request_line(b"POST API/AlarmEvent/EventPush HTTP/1.1")
    assert request.method == "POST"
    assert request.path == "/API/AlarmEvent/EventPush"


def test_conforming_lines_untouched() -> None:
    request = make_request()
    request.parse_request_line(b"POST /API/AlarmEvent/EventPush HTTP/1.1")
    assert request.path == "/API/AlarmEvent/EventPush"

    request = make_request()
    request.parse_request_line(b"GET http://nvr.example.com/x HTTP/1.1")
    assert request.path == "/x"


def test_non_post_methods_not_normalized() -> None:
    # the device only POSTs; other malformed lines stay gunicorn's to reject
    request = make_request()
    with pytest.raises(InvalidRequestLine):
        request.parse_request_line(b"DELETE API/x HTTP/1.1")


def test_garbage_lines_still_rejected() -> None:
    for line in (b"POST", b"POST /x"):
        with pytest.raises(InvalidRequestLine):
            make_request().parse_request_line(line)
