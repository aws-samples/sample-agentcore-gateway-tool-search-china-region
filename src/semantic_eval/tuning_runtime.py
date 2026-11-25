"""Resumable, sanitized accounting and document-only cache for tuning runs."""

from __future__ import annotations

import hashlib
import hmac
import json
import math
import os
import secrets
import stat
import time
from collections import Counter
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from unittest.mock import patch
from urllib.parse import urlsplit, urlunsplit

import httpx
import numpy as np

from semantic_core._http import _HTTPProvider
from semantic_core.providers import (
    EmbeddingProvider,
    EmbeddingResult,
    ModelProfile,
    ProviderError,
    Purpose,
    RequestContext,
    validate_embedding_result,
    validate_texts,
)

CAPS = {"embedding": 1500, "rerank": 1000, "chat": 500}
TOKEN_KEYS = ("prompt_tokens", "completion_tokens", "input_tokens", "output_tokens", "total_tokens")


class RunStopped(BaseException):
    """Stop the entire run without being swallowed by provider degradation."""


def save_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8"
    )
    temporary.replace(path)


def utc_now() -> str:
    """UTC wall-clock metadata; a local timestamp is not external attestation."""
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def endpoint_fingerprint(base_url: str, salt_path: Path, *, create: bool = False) -> str:
    """Bind a validated endpoint to a private, persistent local HMAC salt.

    Keep the salt outside captured artifacts. Losing it prevents resuming a
    frozen run; a digest alone is not an attestation of model identity.
    """
    parts = urlsplit(base_url)
    host = parts.hostname
    if parts.scheme.lower() != "https" or host is None or "?" in base_url or "#" in base_url:
        raise RunStopped("endpoint identity requires a validated HTTPS endpoint")
    host = f"[{host.lower()}]" if ":" in host else host.lower()
    authority = host if parts.port in (None, 443) else f"{host}:{parts.port}"
    normalized = urlunsplit(("https", authority, parts.path.rstrip("/"), "", ""))
    try:
        if create:
            salt_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            try:
                descriptor = os.open(
                    salt_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600
                )
            except FileExistsError:
                pass
            else:
                with os.fdopen(descriptor, "wb") as stream:
                    stream.write(secrets.token_bytes(32))
        descriptor = os.open(salt_path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(descriptor, "rb") as stream:
            metadata = os.fstat(stream.fileno())
            if not stat.S_ISREG(metadata.st_mode) or metadata.st_mode & 0o077:
                raise ValueError("unsafe salt file")
            salt = stream.read(33)
            if len(salt) != 32:
                raise ValueError("invalid salt length")
    except (OSError, ValueError):
        raise RunStopped("missing or invalid private endpoint identity salt") from None
    return hmac.new(salt, normalized.encode("utf-8"), hashlib.sha256).hexdigest()


def read_provenance(path: Path) -> dict[str, Any]:
    """Refuse missing or malformed provenance without changing existing evidence."""
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(value, dict):
            raise ValueError("expected object")
    except (OSError, ValueError):
        raise RunStopped("missing or invalid provenance") from None
    return value


def validate_test_provenance(
    output: Path,
    recommendation: Path,
    *,
    freeze_evidence_ref: str | None = None,
    smoke: bool = False,
) -> dict[str, Any]:
    """Check local bindings and record a caller-supplied external capture reference.

    The reference is a provenance marker, not authenticated evidence or approval.
    An external reviewer must verify its contents and capture time independently.
    Historical runs without these metadata must not be retrofitted or rerun.
    """
    if (output / "test-claim.json").exists():
        raise RunStopped("test already claimed; do not retune or rerun")
    frozen = read_provenance(recommendation)
    manifest = output / "split-manifest.json"
    read_provenance(manifest)
    boundary = read_provenance(output / "freeze-boundary.json")
    finished = frozen.get("dev_finished_at")
    try:
        if not isinstance(finished, str):
            raise ValueError("missing completion")
        timestamp = datetime.fromisoformat(finished)
        if timestamp.tzinfo is None or timestamp.utcoffset() != datetime.now(UTC).utcoffset():
            raise ValueError("completion must be UTC")
        if timestamp > datetime.now(UTC):
            raise ValueError("completion is in the future")
        split_hash = file_sha256(manifest)
        recommendation_hash = file_sha256(recommendation)
    except (ValueError, OSError):
        raise RunStopped("invalid freeze provenance") from None
    if (
        frozen.get("split_manifest_sha256") != split_hash
        or boundary.get("split_manifest_sha256") != split_hash
        or boundary.get("recommendation_sha256") != recommendation_hash
        or boundary.get("dev_finished_at") != finished
        or boundary.get("external_time_attestation") is not False
    ):
        raise RunStopped("mismatched freeze provenance")
    model_path = output / "model-config.json"
    model_hash = frozen.get("model_config_sha256")
    if model_hash is not None or model_path.exists():
        if (
            not model_path.is_file()
            or model_hash != file_sha256(model_path)
            or boundary.get("model_config_sha256") != model_hash
        ):
            raise RunStopped("mismatched model configuration provenance")
    if not smoke and (not isinstance(freeze_evidence_ref, str) or not freeze_evidence_ref.strip()):
        raise RunStopped("external freeze capture reference required before test")
    return {
        "recommendation_sha256": recommendation_hash,
        "split_manifest_sha256": split_hash,
        "dev_finished_at": finished,
        "freeze_evidence_ref": None if smoke else freeze_evidence_ref,
        "external_reference_verified": False,
        "smoke_only": smoke,
        "model_config_sha256": model_hash,
    }


def claim_test(
    output: Path,
    recommendation: Path,
    *,
    freeze_evidence_ref: str | None = None,
    smoke: bool = False,
) -> None:
    """An exclusive claim prevents accidental repeat inspection of the holdout."""
    value = validate_test_provenance(
        output, recommendation, freeze_evidence_ref=freeze_evidence_ref, smoke=smoke
    )
    value["test_claimed_at"] = utc_now()
    try:
        with (output / "test-claim.json").open("x", encoding="utf-8") as stream:
            json.dump(value, stream)
    except FileExistsError:
        raise RunStopped("test already claimed; do not retune or rerun") from None


class Ledger:
    """Count actual HTTP attempts for a serial runner, including prior incomplete ones."""

    def __init__(
        self,
        output: Path,
        *,
        models: Mapping[str, str],
        prices: Mapping[str, Sequence[float]] | None = None,
        caps: Mapping[str, int] = CAPS,
    ) -> None:
        if set(caps) != set(CAPS) or any(
            type(value) is not int or not 0 <= value <= CAPS[kind] for kind, value in caps.items()
        ):
            raise ValueError("invalid or unauthorized call budget")
        if set(models) != set(CAPS):
            raise ValueError("explicit embedding, rerank and chat model names are required")
        for model in models.values():
            ModelProfile(model, "ledger-v1")
        self.models = dict(models)
        self.prices: dict[str, tuple[float, float]] = {}
        for kind, rates in (prices or {}).items():
            if (
                kind not in CAPS
                or not isinstance(rates, list | tuple)
                or len(rates) != 2
                or any(
                    isinstance(rate, bool)
                    or not isinstance(rate, int | float)
                    or not math.isfinite(rate)
                    or rate < 0
                    for rate in rates
                )
            ):
                raise ValueError("prices require nonnegative input/output rates per million tokens")
            self.prices[kind] = (float(rates[0]), float(rates[1]))
        self.output = output
        output.mkdir(parents=True, exist_ok=True)
        self.path = output / "usage-ledger.jsonl"
        accounting_path = output / "accounting-config.json"
        accounting = {
            "models": self.models,
            "prices_cny_per_million_tokens": {
                kind: list(rates) for kind, rates in self.prices.items()
            },
        }
        if accounting_path.exists():
            if read_provenance(accounting_path) != accounting:
                raise RunStopped("persisted accounting configuration mismatch")
        elif self.path.exists() and self.path.stat().st_size:
            raise RunStopped("missing persisted accounting configuration")
        else:
            save_json(accounting_path, accounting)
        self.caps = dict(caps)
        self.phase = "preflight"
        self.rows: list[dict[str, Any]] = []
        self._reload()

    def _reload(self) -> None:
        try:
            rows = (
                [json.loads(line) for line in self.path.read_text().splitlines()]
                if self.path.exists()
                else []
            )
            for sequence, row in enumerate(rows, start=1):
                if (
                    not isinstance(row, dict)
                    or type(row.get("sequence")) is not int
                    or row["sequence"] != sequence
                    or row.get("kind") not in self.caps
                    or row.get("model") != self.models[row["kind"]]
                    or type(row.get("input_count")) is not int
                    or row["input_count"] < 0
                    or type(row.get("complete_response")) is not bool
                    or not isinstance(row.get("usage"), dict)
                    or any(
                        key not in TOKEN_KEYS or type(value) is not int or value < 0
                        for key, value in row["usage"].items()
                    )
                ):
                    raise ValueError("invalid accounting row")
        except (OSError, ValueError, TypeError):
            raise RunStopped("invalid persisted usage ledger") from None
        # Pre-timestamp historical rows stay unchanged; new rows carry both fields.
        self.rows = rows

    def counts(self) -> dict[str, int]:
        self._reload()
        counts = Counter(str(row["kind"]) for row in self.rows)
        return {kind: counts[kind] for kind in self.caps}

    def preflight(self, required: Mapping[str, int], *, persist: bool = True) -> None:
        if any(
            kind not in self.caps or type(count) is not int or count < 0
            for kind, count in required.items()
        ):
            raise ValueError("invalid required call budget")
        counts = self.counts()
        if any(counts[kind] + count > self.caps[kind] for kind, count in required.items()):
            raise RunStopped("call budget exceeded in preflight")
        if persist:
            save_json(
                self.output / f"preflight-{self.phase}.json",
                {
                    "caps": self.caps,
                    "already_attempted": counts,
                    "required_upper_bound": dict(required),
                },
            )

    def _persist(self) -> None:
        temporary = self.path.with_suffix(".tmp")
        temporary.write_text(
            "".join(json.dumps(row, allow_nan=False) + "\n" for row in self.rows),
            encoding="utf-8",
        )
        temporary.replace(self.path)

    def start(self, kind: str, input_count: int) -> dict[str, Any]:
        if kind not in self.caps or type(input_count) is not int or input_count < 0:
            raise ValueError("invalid request accounting")
        if self.counts()[kind] >= self.caps[kind]:
            raise RunStopped("call budget exhausted")
        row: dict[str, Any] = {
            "sequence": len(self.rows) + 1,
            "phase": self.phase,
            "kind": kind,
            "model": self.models[kind],
            "input_count": input_count,
            "started_at": utc_now(),
            "completed_at": None,
            "status": None,
            "complete_response": False,
            "usage": {},
            "usage_status": "missing",
        }
        self.rows.append(row)
        self._persist()
        return row

    def request(self, request: httpx.Request) -> None:
        payload = json.loads(request.content)
        nested = payload.get("input")
        if "documents" in payload:
            kind, inputs = "rerank", payload["documents"]
        elif isinstance(nested, dict) and "documents" in nested:
            kind, inputs = "rerank", nested["documents"]
        elif "input" in payload:
            kind, inputs = "embedding", nested
        else:
            kind, inputs = "chat", payload.get("tools", [])
        if payload.get("model") != self.models[kind]:
            raise RunStopped("request model differs from configured accounting model")
        request.extensions["tuning_row"] = self.start(kind, len(inputs))

    def response(self, response: httpx.Response) -> None:
        row = response.request.extensions["tuning_row"]
        row["status"] = response.status_code
        self._merge(row)

    def _merge(self, row: dict[str, Any]) -> None:
        """Retain attempts written since this row's request began."""
        self._reload()
        for index, existing in enumerate(self.rows):
            if existing["sequence"] == row["sequence"]:
                self.rows[index] = row
                self._persist()
                return
        raise RunStopped("request accounting row missing")

    def finish(self, row: dict[str, Any], body: Mapping[str, object], duration: float) -> None:
        usage = body.get("usage")
        if isinstance(usage, dict):
            row["usage"] = {
                key: usage[key]
                for key in TOKEN_KEYS
                if type(usage.get(key)) is int and usage[key] >= 0
            }
            row["usage_status"] = "reported" if row["usage"] else "missing"
            if any(
                key in usage and (type(usage[key]) is not int or usage[key] < 0)
                for key in TOKEN_KEYS
            ):
                row["usage_status"] = "invalid"
            tokens = row["usage"]
            incoming = tokens.get("prompt_tokens", tokens.get("input_tokens"))
            outgoing = tokens.get("completion_tokens", tokens.get("output_tokens"))
            if row["kind"] in ("embedding", "rerank"):
                outgoing = 0 if outgoing is None else outgoing
            inconsistent = any(
                left in tokens and right in tokens and tokens[left] != tokens[right]
                for left, right in (
                    ("prompt_tokens", "input_tokens"),
                    ("completion_tokens", "output_tokens"),
                )
            )
            if incoming is not None and outgoing is not None and "total_tokens" in tokens:
                inconsistent |= tokens["total_tokens"] != incoming + outgoing
            if row["kind"] in ("embedding", "rerank") and outgoing != 0:
                inconsistent = True
            details = usage.get("prompt_tokens_details")
            if isinstance(details, dict) and "cached_tokens" in details:
                cached = details["cached_tokens"]
                if type(cached) is not int or cached < 0:
                    row["usage_status"] = "invalid"
                else:
                    row["cached_tokens"] = cached
                    inconsistent |= incoming is not None and cached > incoming
            if inconsistent and row["usage_status"] != "invalid":
                row["usage_status"] = "inconsistent"
        elif usage is not None:
            row["usage_status"] = "invalid"
        row["complete_response"] = True
        row["completed_at"] = utc_now()
        row["http_ms"] = duration
        self._merge(row)

    @contextmanager
    def observe(self) -> Iterator[None]:
        """Read numeric usage after bounded HTTP parsing and before adapter checks."""
        original = _HTTPProvider._post
        ledger = self

        def observed(
            provider: _HTTPProvider,
            path: str,
            payload: Mapping[str, object],
            context: RequestContext,
        ) -> dict[str, object]:
            before = len(ledger.rows)
            started = time.perf_counter()
            try:
                body = original(provider, path, payload, context)
                if len(ledger.rows) > before:
                    ledger.finish(ledger.rows[-1], body, (time.perf_counter() - started) * 1000)
                return body
            except ProviderError as exc:
                if len(ledger.rows) > before:
                    row = ledger.rows[-1]
                    row["transport_error"] = exc.code.value
                    row["completed_at"] = utc_now()
                    row["http_ms"] = (time.perf_counter() - started) * 1000
                    ledger._merge(row)
                raise

        with patch.object(_HTTPProvider, "_post", observed):
            yield

    def summary(self) -> dict[str, Any]:
        self._reload()
        usage: dict[str, dict[str, int]] = {}
        cost = 0.0
        unknown = 0
        for row in self.rows:
            kind, tokens = row["kind"], row["usage"]
            totals = usage.setdefault(kind, {})
            for key, value in tokens.items():
                totals[key] = totals.get(key, 0) + value
            incoming = tokens.get("prompt_tokens", tokens.get("input_tokens"))
            outgoing = tokens.get("completion_tokens", tokens.get("output_tokens"))
            if kind in ("embedding", "rerank"):
                incoming = incoming if incoming is not None else tokens.get("total_tokens")
                outgoing = 0
            if (
                incoming is None
                or outgoing is None
                or not row["complete_response"]
                or row.get("usage_status", "reported") != "reported"
                or kind not in self.prices
            ):
                unknown += 1
                continue
            rate_in, rate_out = self.prices[kind]
            cost += (incoming * rate_in + outgoing * rate_out) / 1_000_000
        return {
            "attempts": self.counts(),
            "caps": self.caps,
            "reported_usage": usage,
            "public_list_price_cny": None if unknown else round(cost, 10),
            "known_public_list_price_cny": round(cost, 10),
            "unknown_cost_requests": unknown,
            "models": self.models,
            "prices_cny_per_million_tokens": self.prices,
            "cost_scope": "Caller-configured rates; missing prices remain unknown, not an invoice.",
        }


class DocumentCache:
    """Cache validated public synthetic document vectors, keyed by full profile."""

    def __init__(self, provider: EmbeddingProvider, directory: Path) -> None:
        self.provider = provider
        self.directory = directory
        directory.mkdir(parents=True, exist_ok=True)
        self.hits = 0

    def describe(self) -> ModelProfile:
        return self.provider.describe()

    def _path(self, text: str, profile: ModelProfile | None = None) -> Path:
        identity = json.dumps(
            [asdict(profile or self.describe()), "document", text], sort_keys=True
        )
        return self.directory / (hashlib.sha256(identity.encode()).hexdigest() + ".npy")

    def embed(
        self, texts: Sequence[str], purpose: Purpose, context: RequestContext
    ) -> EmbeddingResult:
        context.remaining()
        validate_texts(texts, purpose)
        if purpose == "query":
            return self.provider.embed(texts, purpose, context)
        profile = self.describe()
        missing = list(
            dict.fromkeys(text for text in texts if not self._path(text, profile).exists())
        )
        usage: dict[str, int] = {}
        if missing:
            result = self.provider.embed(missing, purpose, context)
            vectors = validate_embedding_result(result, profile, len(missing))
            context.remaining()
            usage.update(result.usage)
            for text, vector in zip(missing, vectors, strict=True):
                path = self._path(text, profile)
                temporary = path.with_suffix(".tmp")
                with temporary.open("wb") as stream:
                    np.save(stream, vector, allow_pickle=False)
                temporary.replace(path)
        self.hits += len(texts) - len(missing)
        matrix = np.stack(
            [np.load(self._path(text, profile), allow_pickle=False) for text in texts]
        )
        result = EmbeddingResult(matrix, profile, usage)
        validate_embedding_result(result, profile, len(texts))
        context.remaining()
        return result
