"""Gunicorn configuration for the gateway (passed via ``--config``).

Installs :class:`~nvr_gateway.device_http.DeviceRequest` so the NVR's
non-conforming request lines (missing leading slash) are accepted; see
that module for details.
"""

from gunicorn.http.parser import RequestParser

from nvr_gateway.device_http import DeviceRequest

# gunicorn declares `mesg_class = Request` (inferred as the exact class,
# not type[Request]); assigning a subclass is the intended extension use.
RequestParser.mesg_class = DeviceRequest  # ty: ignore[invalid-assignment]
