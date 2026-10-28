"""Exceptions raised by the Freshservice client and command-line interface."""

from __future__ import annotations


class FreshserviceError(Exception):
    """Base class for expected Freshservice CLI errors."""


class CredentialError(FreshserviceError):
    """The Freshservice API key could not be loaded from Keychain."""


class NetworkError(FreshserviceError):
    """The Freshservice endpoint could not be reached."""


class ResponseDecodeError(FreshserviceError):
    """Freshservice returned a successful response that was not valid JSON."""


class ApiError(FreshserviceError):
    """Freshservice returned a non-success HTTP response."""

    def __init__(self, status_code: int, message: str) -> None:
        self.status_code = status_code
        self.message = message
        super().__init__(f"Freshservice API error ({status_code}): {message}")


class WriteDisabledError(FreshserviceError):
    """A write was attempted while the client was in safe read-only mode."""

    def __init__(self) -> None:
        super().__init__(
            "write operations are disabled; construct the client with "
            "allow_writes=True to opt in"
        )
