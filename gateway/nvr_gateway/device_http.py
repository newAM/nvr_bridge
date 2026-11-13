"""Tolerance for the NVR's non-conforming HTTP request line.

Captures show the device sends request lines without the leading slash,
e.g. ``POST API/AlarmEvent/EventPush HTTP/1.1``.  RFC 9112 allows only
origin-form (``/path``) or absolute-form (``scheme://...``) targets, so
gunicorn answers these with 400 and the pushes would be lost.
:class:`DeviceRequest` normalizes the request line to origin-form before
gunicorn's own (otherwise stock) validation runs.

The normalization is narrow: only POST targets that are neither
origin-form, absolute-form, nor asterisk-form get a ``/`` prefix; every
other check (method token, sizes, headers, smuggling defences) remains
gunicorn's.
"""

from gunicorn.http.message import Request


class DeviceRequest(Request):
    """A stock gunicorn ``Request`` that accepts the NVR's missing leading slash."""

    def parse_request_line(self, line_bytes: bytes) -> None:
        bits = line_bytes.split(b" ", 2)
        if (
            len(bits) == 3
            and bits[0] == b"POST"
            and not bits[1].startswith((b"/", b"*"))
            and b"://" not in bits[1]
        ):
            super().parse_request_line(b" ".join((bits[0], b"/" + bits[1], bits[2])))
        else:
            super().parse_request_line(line_bytes)
