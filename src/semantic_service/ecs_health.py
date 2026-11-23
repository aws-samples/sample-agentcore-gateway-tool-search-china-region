"""Credential-free loopback readiness probe; deliberately standard-library only."""

from __future__ import annotations

import http.client
import os
import ssl
from contextlib import closing


def healthcheck(port: int) -> int:
    # Python 3.12 HTTPSConnection verifies certificates by default. This local
    # readiness-only probe intentionally trusts the process-local self-signed
    # certificate: its destination is fixed loopback, with no credentials/data.
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    try:
        with closing(
            # nosemgrep: httpsconnection-detected
            http.client.HTTPSConnection("127.0.0.1", port, timeout=2, context=context)
        ) as client:
            client.request("GET", "/ready")
            response = client.getresponse()
            response.read(1024)
            return 0 if response.status == 200 else 1
    except Exception:
        return 1


if __name__ == "__main__":
    raise SystemExit(healthcheck(int(os.environ.get("SEMSEARCH_ECS_HEALTH_PORT", "9443"))))
