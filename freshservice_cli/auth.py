"""Credential loading for Freshservice."""

from __future__ import annotations

import subprocess
from collections.abc import Callable, Sequence

from .errors import CredentialError

DEFAULT_KEYCHAIN_ACCOUNT = "api-key"
DEFAULT_KEYCHAIN_SERVICE = "freshservice.api-key"

Runner = Callable[..., subprocess.CompletedProcess[str]]


def load_api_key(
    *,
    account: str = DEFAULT_KEYCHAIN_ACCOUNT,
    service: str = DEFAULT_KEYCHAIN_SERVICE,
    runner: Runner = subprocess.run,
) -> str:
    """Read an API key from the macOS Keychain using ``security``.

    The runner is injectable so unit tests never need to access a real
    Keychain. The key is returned only to the caller and is never printed.
    """

    command: Sequence[str] = (
        "security",
        "find-generic-password",
        "-a",
        account,
        "-s",
        service,
        "-w",
    )

    try:
        result = runner(
            list(command),
            check=True,
            capture_output=True,
            text=True,
        )
    except FileNotFoundError as exc:
        raise CredentialError("the macOS security command is not available") from exc
    except subprocess.CalledProcessError as exc:
        raise CredentialError(
            "the Freshservice API key was not found in the macOS Keychain"
        ) from exc

    api_key = result.stdout.strip()
    if not api_key:
        raise CredentialError(
            "the macOS Keychain returned an empty Freshservice API key"
        )
    return api_key
