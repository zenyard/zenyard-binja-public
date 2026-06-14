from __future__ import annotations
import sys


from .configuration import get_api_key, get_api_url
from .zenyard_client import ApiClient, Configuration


def make_client() -> ApiClient:
    config = Configuration(
        host=get_api_url(),
        api_key={"APIKeyHeader": get_api_key()},
    )
    config.verify_ssl = True
    if sys.platform.lower() == "darwin":
        config.ssl_ca_cert = "/etc/ssl/cert.pem"
    return ApiClient(configuration=config)
