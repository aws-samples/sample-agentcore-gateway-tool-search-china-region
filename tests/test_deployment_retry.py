"""Offline coverage for persistent deployment request budgets and recovery."""

import hashlib
import json
from datetime import UTC, datetime, timedelta, timezone

import pytest
from botocore.exceptions import ClientError, ReadTimeoutError
from scripts import deployment_retry as retry


def request_key(service, operation, parameters):
    return hashlib.sha256(retry.canonical([service, operation, parameters])).hexdigest()


def timeout():
    return ReadTimeoutError(endpoint_url="https://offline.invalid", response=None)


def interrupted_sleep(_):
    raise InterruptedError("offline process interruption")


def test_real_botocore_timeout_with_none_response_is_retryable():
    error = timeout()
    assert error.response is None
    assert retry.error_code(error) == "ReadTimeoutError"
    assert retry.transient(error) is True


@pytest.mark.parametrize(
    "response",
    [
        None,
        [],
        "invalid",
        1,
        {"Error": None, "ResponseMetadata": None},
        {"Error": [], "ResponseMetadata": "invalid"},
        {"Error": {"Code": []}, "ResponseMetadata": {"HTTPStatusCode": "503"}},
        {"ResponseMetadata": {"HTTPStatusCode": []}},
        {"ResponseMetadata": {"HTTPStatusCode": {}}},
    ],
)
def test_malformed_error_response_is_safe_and_nontransient(response):
    error = RuntimeError("offline failure")
    error.response = response
    assert retry.error_section(error, "Error") == (
        response["Error"]
        if isinstance(response, dict) and isinstance(response.get("Error"), dict)
        else {}
    )
    assert retry.error_code(error) == "RuntimeError"
    assert retry.transient(error) is False


@pytest.mark.parametrize(
    ("code", "status", "expected"),
    [
        ("ThrottlingException", 400, True),
        ("UnknownServiceFailure", 503, True),
        ("ValidationException", 400, False),
    ],
)
def test_real_botocore_client_error_classification(code, status, expected):
    error = ClientError(
        {"Error": {"Code": code}, "ResponseMetadata": {"HTTPStatusCode": status}},
        "OfflineOperation",
    )
    assert retry.error_code(error) == code
    assert retry.transient(error) is expected


@pytest.mark.parametrize(
    ("code", "status", "expected"),
    [
        ("408", 408, True),
        ("429", 429, True),
        ("UnknownTimeout", 408, True),
        ("UnknownThrottling", 429, True),
        ("408", 401, False),
        ("429", 403, False),
        ("408", 400, False),
        ("429", 400, False),
        ("408", None, False),
        ("429", None, False),
        ("408", "408", False),
        ("429", "429", False),
        ("UnauthorizedException", 401, False),
        ("AccessDeniedException", 403, False),
        ("ValidationException", 400, False),
    ],
)
def test_http_timeout_and_throttle_status_without_numeric_code_allowlist(code, status, expected):
    error = ClientError(
        {"Error": {"Code": code}, "ResponseMetadata": {"HTTPStatusCode": status}},
        "OfflineOperation",
    )
    assert retry.transient(error) is expected


@pytest.mark.parametrize("status", [408, 429])
def test_http_timeout_and_throttle_are_retried_within_the_same_request(tmp_path, status):
    journal = retry.Journal(tmp_path / "operations.jsonl")
    calls = []

    def request():
        calls.append("request")
        if len(calls) == 1:
            raise ClientError(
                {"Error": {"Code": str(status)}, "ResponseMetadata": {"HTTPStatusCode": status}},
                "OfflineOperation",
            )
        return {"completed": True}

    result = retry.Caller(journal, sleep=lambda _: None, now=lambda: 0).call(
        "service", "operation", {}, request
    )
    assert result == {"completed": True}
    attempts = [row for row in journal.rows() if row["event"] == "attempt"]
    assert [row["attempt"] for row in attempts] == [1, 2]
    assert len({row["key"] for row in attempts}) == 1


@pytest.mark.parametrize("status", [401, 403])
@pytest.mark.parametrize("code", ["ThrottlingException", "RequestTimeout"])
def test_authentication_denial_takes_precedence_over_retryable_code(status, code):
    error = ClientError(
        {"Error": {"Code": code}, "ResponseMetadata": {"HTTPStatusCode": status}},
        "OfflineOperation",
    )
    assert retry.transient(error) is False


@pytest.mark.parametrize(
    "code",
    [
        "AccessDenied",
        "AccessDeniedException",
        "UnauthorizedException",
        "ExpiredToken",
        "ExpiredTokenException",
        "InvalidClientTokenId",
        "UnrecognizedClientException",
        "SignatureDoesNotMatch",
        "InvalidSignatureException",
        "ValidationError",
        "ValidationException",
    ],
)
@pytest.mark.parametrize("status", [408, 429, 503])
def test_explicit_rejection_code_takes_precedence_over_retryable_status(code, status):
    error = ClientError(
        {"Error": {"Code": code}, "ResponseMetadata": {"HTTPStatusCode": status}},
        "OfflineOperation",
    )
    assert retry.transient(error) is False


@pytest.mark.parametrize("status", [600, 999])
def test_only_actual_5xx_statuses_are_server_failures(status):
    error = ClientError(
        {"Error": {"Code": "UnknownFailure"}, "ResponseMetadata": {"HTTPStatusCode": status}},
        "OfflineOperation",
    )
    assert retry.transient(error) is False


def test_403_conflicting_with_throttling_checks_landed_once_without_retry(tmp_path):
    events = []

    def write():
        events.append("write")
        raise ClientError(
            {
                "Error": {"Code": "ThrottlingException"},
                "ResponseMetadata": {"HTTPStatusCode": 403},
            },
            "OfflineOperation",
        )

    def landed():
        events.append("check")
        return None

    with pytest.raises(RuntimeError, match="ThrottlingException"):
        retry.Caller(
            retry.Journal(tmp_path / "operations.jsonl"),
            sleep=lambda _: events.append("wait"),
            now=lambda: 0,
        ).call("service", "operation", {}, write, recover=landed, mutating=True)
    assert events == ["write", "check"]


def test_canonical_preserves_existing_request_key_bytes():
    value = [
        "service",
        "operation",
        {"z": "值", "a": datetime(2026, 10, 16, 8, tzinfo=timezone(timedelta(hours=8)))},
    ]
    expected = b'["service","operation",{"a":"2026-10-16T00:00:00+00:00","z":"\\u503c"}]'
    assert retry.canonical(value) == expected
    assert retry.serial(datetime(2026, 10, 16, tzinfo=UTC)) == "2026-10-16T00:00:00+00:00"


@pytest.mark.parametrize("mutating", [False, True])
def test_five_attempt_limit_survives_caller_and_journal_restart(tmp_path, mutating):
    path = tmp_path / "operations.jsonl"
    calls = []

    def fail():
        calls.append("request")
        raise timeout()

    with pytest.raises(InterruptedError):
        retry.Caller(retry.Journal(path), sleep=interrupted_sleep, now=lambda: 0).call(
            "service", "operation", {"id": "fixed"}, fail, mutating=mutating
        )
    for _ in range(2):
        with pytest.raises(RuntimeError):
            retry.Caller(retry.Journal(path), sleep=lambda _: None, now=lambda: 0).call(
                "service", "operation", {"id": "fixed"}, fail, mutating=mutating
            )
    assert len(calls) == 5
    attempts = [row for row in retry.Journal(path).rows() if row["event"] == "attempt"]
    assert [row["attempt"] for row in attempts] == [1, 2, 3, 4, 5]
    assert {row["key"] for row in attempts} == {
        request_key("service", "operation", {"id": "fixed"})
    }


@pytest.mark.parametrize("mutating", [False, True])
@pytest.mark.parametrize("resumed_clock", [601, -1])
def test_time_budget_survives_restart_and_rejects_clock_rollback(tmp_path, mutating, resumed_clock):
    path = tmp_path / "operations.jsonl"
    calls = []

    def fail():
        calls.append("request")
        raise timeout()

    with pytest.raises(InterruptedError):
        retry.Caller(retry.Journal(path), sleep=interrupted_sleep, now=lambda: 0).call(
            "service", "operation", {}, fail, mutating=mutating
        )
    original = path.read_bytes()
    with pytest.raises(RuntimeError, match="Bounded retries exhausted"):
        retry.Caller(retry.Journal(path), now=lambda: resumed_clock).call(
            "service", "operation", {}, fail, mutating=mutating
        )
    assert calls == ["request"]
    assert path.read_bytes() == original


@pytest.mark.parametrize("resumed", [False, True])
def test_before_attempt_cannot_cross_the_original_budget(tmp_path, resumed):
    journal = retry.Journal(tmp_path / "operations.jsonl")
    parameters = {"id": "fixed"}
    key = request_key("service", "operation", parameters)
    if resumed:
        journal.emit({"event": "attempt", "key": key, "attempt": 1, "clock": 0})
    original = journal.rows()
    clock = [599 if resumed else 0]
    calls = []

    def before():
        clock[0] = 602 if resumed else 601

    with pytest.raises(RuntimeError, match="Bounded retries exhausted"):
        retry.Caller(journal, now=lambda: clock[0]).call(
            "service",
            "operation",
            parameters,
            lambda: calls.append("write"),
            mutating=True,
            before_attempt=before,
        )
    assert not calls
    assert journal.rows() == original


def test_first_precheck_time_stays_in_persisted_budget(tmp_path):
    journal = retry.Journal(tmp_path / "operations.jsonl")
    clock = [0]
    calls = []

    def before():
        clock[0] += 301

    def write():
        calls.append(clock[0])
        raise timeout()

    def wait(_):
        clock[0] = 599

    with pytest.raises(RuntimeError, match="Bounded retries exhausted"):
        retry.Caller(journal, now=lambda: clock[0], sleep=wait).call(
            "service", "operation", {}, write, mutating=True, before_attempt=before
        )
    assert calls == [301]
    assert [row["clock"] for row in journal.rows() if row["event"] == "attempt"] == [0]


def test_before_attempt_may_finish_at_exactly_600_seconds(tmp_path):
    journal = retry.Journal(tmp_path / "operations.jsonl")
    key = request_key("service", "operation", {})
    journal.emit({"event": "attempt", "key": key, "attempt": 1, "clock": 0})
    clock = [599]

    def before():
        clock[0] = 600

    result = retry.Caller(journal, now=lambda: clock[0]).call(
        "service", "operation", {}, lambda: "complete", before_attempt=before, mutating=True
    )
    assert result == "complete"


@pytest.mark.parametrize("error", [timeout(), ValueError("ambiguous offline conflict")])
@pytest.mark.parametrize("previous_attempts", [0, 4])
def test_landed_check_prevents_replay_even_on_final_or_nontransient_failure(
    tmp_path, error, previous_attempts
):
    journal = retry.Journal(tmp_path / "operations.jsonl")
    parameters = {"id": "same-immutable-target"}
    key = request_key("service", "operation", parameters)
    for attempt in range(previous_attempts):
        journal.emit({"event": "attempt", "key": key, "attempt": attempt + 1, "clock": 0})
    events = []

    def write():
        events.append("write")
        raise error

    def landed():
        events.append("check same target")
        return {}

    result = retry.Caller(journal, sleep=lambda _: events.append("sleep"), now=lambda: 0).call(
        "service", "operation", parameters, write, recover=landed, mutating=True
    )
    assert result == {}
    assert events == ["write", "check same target"]
    assert journal.rows()[-1]["event"] == "recovered"


def test_retry_follows_failed_landed_check(tmp_path):
    journal = retry.Journal(tmp_path / "operations.jsonl")
    events = []

    def write():
        events.append("write")
        if events.count("write") == 1:
            raise timeout()
        return {"completed": True}

    def landed():
        events.append("check")
        return None

    result = retry.Caller(journal, sleep=lambda _: events.append("wait"), now=lambda: 0).call(
        "service", "operation", {}, write, recover=landed, mutating=True
    )
    assert result == {"completed": True}
    assert events == ["write", "check", "wait", "write"]


def test_nontransient_unlanded_failure_does_not_retry(tmp_path):
    events = []

    def write():
        events.append("write")
        raise ValueError("invalid request")

    def landed():
        events.append("check")
        return None

    with pytest.raises(RuntimeError, match="ValueError"):
        retry.Caller(
            retry.Journal(tmp_path / "operations.jsonl"),
            sleep=lambda _: events.append("wait"),
        ).call("service", "operation", {}, write, recover=landed, mutating=True)
    assert events == ["write", "check"]


@pytest.mark.parametrize("terminal", ["success", "missing", "recovered"])
def test_only_completed_reads_reset_their_attempt_chain(tmp_path, terminal):
    journal = retry.Journal(tmp_path / "operations.jsonl")
    key = request_key("service", "operation", {})
    journal.emit({"event": "attempt", "key": key, "attempt": 1, "clock": 0})
    journal.emit({"event": terminal, "key": key})
    assert journal.attempts(key, mutating=False) == []
    assert len(journal.attempts(key, mutating=True)) == 1


def test_missing_response_is_recorded_without_retry(tmp_path):
    journal = retry.Journal(tmp_path / "operations.jsonl")
    calls = []

    def read():
        calls.append("read")
        raise ClientError({"Error": {"Code": "NotFound"}}, "OfflineRead")

    result = retry.Caller(journal, sleep=lambda _: calls.append("wait")).call(
        "service", "operation", {}, read, missing=("NotFound",)
    )
    assert result is None
    assert calls == ["read"]
    assert journal.rows()[-1]["event"] == "missing"


def test_attempt_is_persisted_before_the_request(tmp_path):
    path = tmp_path / "operations.jsonl"

    def request():
        row = json.loads(path.read_text().strip())
        assert row["event"] == "attempt"
        assert row["clock"] == 123
        return "complete"

    assert (
        retry.Caller(retry.Journal(path), now=lambda: 123).call("service", "operation", {}, request)
        == "complete"
    )
