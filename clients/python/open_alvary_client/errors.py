"""What the client raises: a refusal Open Alvary answered, or Open Alvary out of reach with nothing in
the cache that may answer instead."""


class OpenAlvaryError(Exception):
    """Something Open Alvary refused, or could not be reached for."""


class Refused(OpenAlvaryError):
    """Open Alvary answered with a problem document: ``code`` is one from the errors page."""

    def __init__(self, status: int, code: str | None, detail: str | None = None):
        super().__init__(f"{status} {code or 'refused'}: {detail or 'Request refused.'}")
        self.status, self.code, self.detail = status, code, detail


class RateLimited(Refused):
    """Still 429 after the bounded attempts. ``retry_after`` is the server's wait in seconds, if it said."""

    def __init__(self, code: str | None, detail: str | None, retry_after: float | None):
        super().__init__(429, code, detail)
        self.retry_after = retry_after


class Unavailable(OpenAlvaryError):
    """Open Alvary could not be reached (a network error, a timeout or a 5xx after the bounded attempts)
    and nothing cached may answer instead."""


class CacheExpired(Unavailable):
    """Open Alvary could not be reached and the change feed has not been read for seven days, so the
    cache no longer answers."""
