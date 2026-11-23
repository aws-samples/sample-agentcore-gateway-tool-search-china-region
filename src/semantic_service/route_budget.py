"""Fixed private routing limits shared by the facade and its two backends.

These are reviewed code constants, not environment configuration. Provider
budgets remain owned by the existing Search implementation.
"""

import math

ROUTE_SECONDS = 14.0
ECS_SECONDS = 4.0
FALLBACK_SECONDS = 10.0
# Reserve enough of the route window to attempt Lambda fallback, including cold
# initialization and invocation overhead. This allowance is not a latency SLO.
FALLBACK_RESERVE_SECONDS = 8.0
FACADE_TIMEOUT_SECONDS = 15
FALLBACK_TIMEOUT_SECONDS = 11
PLATFORM_RESERVE_SECONDS = 0.15
MIN_ATTEMPT_SECONDS = 0.5
CLOCK_SKEW_SECONDS = 1.0
# ECS reserves this part of its existing HTTP window for a completed result to
# cross process/HTTP boundaries; it never extends the facade listening window.
RESULT_DELIVERY_SECONDS = 0.5


def validate_budgets() -> None:
    """Reject inconsistent reviewed constants before runtime or CDK can use them."""
    values = (
        ROUTE_SECONDS,
        ECS_SECONDS,
        FALLBACK_SECONDS,
        FALLBACK_RESERVE_SECONDS,
        FACADE_TIMEOUT_SECONDS,
        FALLBACK_TIMEOUT_SECONDS,
        PLATFORM_RESERVE_SECONDS,
        MIN_ATTEMPT_SECONDS,
        CLOCK_SKEW_SECONDS,
        RESULT_DELIVERY_SECONDS,
    )
    if any(
        type(value) not in (int, float) or not math.isfinite(value) or value <= 0
        for value in values
    ):
        raise ValueError("invalid routing budget")
    if not (
        MIN_ATTEMPT_SECONDS < ECS_SECONDS
        and RESULT_DELIVERY_SECONDS + MIN_ATTEMPT_SECONDS < ECS_SECONDS
        and CLOCK_SKEW_SECONDS <= 1.0
        and MIN_ATTEMPT_SECONDS <= FALLBACK_RESERVE_SECONDS <= FALLBACK_SECONDS
        and FALLBACK_RESERVE_SECONDS < FALLBACK_TIMEOUT_SECONDS < FACADE_TIMEOUT_SECONDS
        and ECS_SECONDS + FALLBACK_SECONDS <= ROUTE_SECONDS
        and ROUTE_SECONDS + PLATFORM_RESERVE_SECONDS <= FACADE_TIMEOUT_SECONDS
        and FALLBACK_SECONDS + PLATFORM_RESERVE_SECONDS <= FALLBACK_TIMEOUT_SECONDS
    ):
        raise ValueError("inconsistent routing budget")


validate_budgets()
