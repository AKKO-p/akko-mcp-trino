"""The status of a context source in a response: one word per adapter."""

from .errors import ProviderRefused, ProviderUnavailable

OK = "ok"
NOT_FOUND = "not_found"
NOT_MAPPED = "not_mapped"
PARTIAL = "partial"
UNAVAILABLE = "unavailable"
REFUSED = "refused"
ERROR = "error"
NOT_CHECKED = "not_checked"


def status_of(exc: BaseException) -> str:
    """Return the status an exception stands for, without any of its text."""
    if isinstance(exc, ProviderRefused):
        return REFUSED
    if isinstance(exc, (ProviderUnavailable, TimeoutError)):
        return UNAVAILABLE
    return ERROR
