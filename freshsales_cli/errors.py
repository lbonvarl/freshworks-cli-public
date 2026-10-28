"""Exception types used by the Freshsales CLI."""


class FreshsalesError(Exception):
    """Base class for expected application errors."""


class CredentialError(FreshsalesError):
    """The API key could not be loaded from the configured credential store."""


class NetworkError(FreshsalesError):
    """The API could not be reached."""


class ResponseDecodeError(FreshsalesError):
    """The API returned a response that was not valid JSON."""


class ApiError(FreshsalesError):
    """Freshsales returned a non-success HTTP status."""

    def __init__(self, status_code: int, message: str):
        self.status_code = status_code
        self.message = message
        super().__init__(f"Freshsales API error ({status_code}): {message}")


class PartialWriteError(FreshsalesError):
    """A multi-request write stopped part-way; ``report`` lists what was sent."""

    def __init__(self, message: str, report: dict):
        self.report = report
        super().__init__(message)


class WriteDisabledError(FreshsalesError):
    """A mutating request was attempted while writes are disabled."""

    def __init__(self):
        super().__init__(
            "write operations are disabled; write commands only send requests "
            "when run with --apply"
        )
