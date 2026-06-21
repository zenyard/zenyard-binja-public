from __future__ import annotations
import sys

from urllib3.util.retry import Retry

from .configuration import get_api_key, get_api_url
from .zenyard_client import ApiClient, Configuration

DEFAULT_REQUEST_TIMEOUT: tuple[float, float] = (5.0, 30.0)
LARGE_UPLOAD_TIMEOUT: float = 300.0


class TimeoutApiClient(ApiClient):
    """Injects ``DEFAULT_REQUEST_TIMEOUT``; explicit per-call values win.

    This is the only seam that works: the generated ``rest.py`` passes
    ``timeout=None`` unconditionally to the pool manager when no per-call
    timeout is given, and urllib3 treats an explicit None as "block forever" —
    so a pool-level default would never apply.
    """

    def call_api(
        self,
        method,
        url,
        header_params=None,
        body=None,
        post_params=None,
        _request_timeout=None,
    ):
        return super().call_api(
            method,
            url,
            header_params,
            body,
            post_params,
            _request_timeout or DEFAULT_REQUEST_TIMEOUT,
        )


def make_client() -> ApiClient:
    config = Configuration(
        host=get_api_url(),
        api_key={"APIKeyHeader": get_api_key()},
    )
    config.verify_ssl = True
    if sys.platform.lower() == "darwin":
        config.ssl_ca_cert = "/etc/ssl/cert.pem"

    # disable transport-level error retries except redirect
    config.retries = Retry(connect=0, read=0, status=0, other=1, redirect=3)
    return TimeoutApiClient(configuration=config)
