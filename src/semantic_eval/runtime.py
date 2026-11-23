"""Explicit real-mode prerequisites; secrets remain in process memory.

Only the designated AWS CLI profile and Secrets Manager secret are used. There
is no credential fallback, environment-key lookup, file write, or logging.
"""

from __future__ import annotations

import json
import os
import subprocess  # nosec B404
from dataclasses import dataclass, field
from typing import cast
from urllib.parse import urlsplit

from semantic_core.openai_compatible import OpenAICompatibleConfig


@dataclass(frozen=True)
class Credentials:
    base_url: str = field(repr=False)
    api_key: str = field(repr=False)


class RealModeUnavailable(Exception):
    """A fixed safe failure, with no subprocess or secret details attached."""

    def __init__(self) -> None:
        super().__init__("real model prerequisites are unavailable")


class CredentialsExpired(RealModeUnavailable):
    """A distinct safe stop signal: refresh credentials before any further calls."""

    def __init__(self) -> None:
        Exception.__init__(self, "AWS credentials expired; refresh the configured profile")


def _json_object(text: str) -> dict[str, object] | None:
    try:
        decoded: object = json.loads(text)
    except (ValueError, UnicodeError):
        return None
    if not isinstance(decoded, dict):
        return None
    return cast(dict[str, object], decoded)


def _selection() -> tuple[list[str], str]:
    profile = os.environ.get("SEMANTIC_EVAL_AWS_PROFILE", "sample-semantic-eval")
    region = os.environ.get("SEMANTIC_EVAL_AWS_REGION", "cn-north-1")
    secret = os.environ.get("SEMANTIC_EVAL_SECRET_ID", "semsearch/model-api")
    for value in (profile, region, secret):
        if (
            not value
            or value.startswith("-")
            or len(value) > 2048
            or any(ord(character) <= 32 or ord(character) > 126 for character in value)
        ):
            raise RealModeUnavailable()
    return [
        "--profile",
        profile,
        "--region",
        region,
        "--output",
        "json",
        "--no-cli-pager",
    ], secret


def _aws_json(arguments: list[str], *, flags: list[str] | None = None) -> dict[str, object]:
    if flags is None:
        flags, _ = _selection()
    completed: subprocess.CompletedProcess[str] | None = None
    try:
        completed = subprocess.run(  # nosec B603, B607
            ["aws", *arguments, *flags],
            capture_output=True,
            text=True,
            check=False,
            timeout=20,
            stdin=subprocess.DEVNULL,
        )
    except (OSError, subprocess.SubprocessError, UnicodeError):
        # Raise outside the except block so even __context__ cannot retain
        # subprocess errors that may include credentials in stdout/stderr.
        pass
    if completed is not None and completed.returncode != 0:
        # Inspect stderr only for fixed error markers in memory. Never retain its
        # contents in a message, chained exception, report, or log.
        if any(
            marker in completed.stderr
            for marker in ("ExpiredToken", "RequestExpired", "TokenRefreshRequired")
        ):
            raise CredentialsExpired()
    if completed is None or completed.returncode != 0:
        raise RealModeUnavailable()
    decoded = _json_object(completed.stdout)
    if decoded is None:
        raise RealModeUnavailable()
    return decoded


def _credentials(secret: dict[str, object]) -> Credentials | None:
    raw = secret.get("SecretString")
    if not isinstance(raw, str):
        return None
    decoded = _json_object(raw)
    if decoded is None:
        return None
    base_url, api_key = decoded.get("base_url"), decoded.get("api_key")
    if (
        not isinstance(base_url, str)
        or not isinstance(api_key, str)
        or not api_key.strip()
        or any(ord(character) < 32 or ord(character) > 126 for character in api_key)
    ):
        return None
    try:
        hostname = urlsplit(base_url).hostname
        if hostname is None:
            return None
        # Apply the same HTTPS origin/prefix validation as the shared adapters.
        OpenAICompatibleConfig(base_url, (hostname,))
    except ValueError:
        return None
    return Credentials(base_url=base_url, api_key=api_key)


def load_credentials() -> Credentials:
    """Check STS first, then read exactly the approved secret without retries."""
    flags, secret_id = _selection()
    identity = _aws_json(["sts", "get-caller-identity"], flags=flags)
    if any(
        not isinstance(identity.get(key), str) or not cast(str, identity[key]).strip()
        for key in ("Account", "Arn", "UserId")
    ):
        raise RealModeUnavailable()
    secret = _aws_json(
        ["secretsmanager", "get-secret-value", "--secret-id", secret_id], flags=flags
    )
    credentials = _credentials(secret)
    if credentials is None:
        raise RealModeUnavailable()
    return credentials


def preflight_real() -> Credentials:
    """Return validated in-memory credentials or a safe unavailable error."""
    return load_credentials()
