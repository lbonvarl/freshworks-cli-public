from __future__ import annotations

import subprocess
import unittest

from freshsales_cli.auth import load_api_key
from freshsales_cli.errors import CredentialError


class KeychainTests(unittest.TestCase):
    def test_load_api_key_uses_expected_security_command(self) -> None:
        calls: list[tuple[list[str], dict[str, object]]] = []

        def runner(
            command: list[str], **kwargs: object
        ) -> subprocess.CompletedProcess[str]:
            calls.append((command, kwargs))
            return subprocess.CompletedProcess(
                command, 0, stdout="test-key\n", stderr=""
            )

        self.assertEqual(load_api_key(runner=runner), "test-key")
        self.assertEqual(
            calls,
            [
                (
                    [
                        "security",
                        "find-generic-password",
                        "-a",
                        "api-key",
                        "-s",
                        "freshsales.api-key",
                        "-w",
                    ],
                    {"check": True, "capture_output": True, "text": True},
                )
            ],
        )

    def test_load_api_key_allows_custom_account_and_service(self) -> None:
        command_seen: list[str] = []

        def runner(command: list[str], **_: object) -> subprocess.CompletedProcess[str]:
            command_seen.extend(command)
            return subprocess.CompletedProcess(command, 0, stdout="key", stderr="")

        self.assertEqual(
            load_api_key(account="account", service="service", runner=runner),
            "key",
        )
        self.assertEqual(
            command_seen,
            [
                "security",
                "find-generic-password",
                "-a",
                "account",
                "-s",
                "service",
                "-w",
            ],
        )

    def test_missing_key_is_reported_without_leaking_process_output(self) -> None:
        def runner(command: list[str], **_: object) -> subprocess.CompletedProcess[str]:
            raise subprocess.CalledProcessError(
                returncode=44,
                cmd=command,
                stderr="security: The specified item could not be found.",
            )

        with self.assertRaisesRegex(CredentialError, "not found"):
            load_api_key(runner=runner)

    def test_missing_security_command_is_reported(self) -> None:
        def runner(command: list[str], **_: object) -> subprocess.CompletedProcess[str]:
            raise FileNotFoundError("security")

        with self.assertRaisesRegex(CredentialError, "security command"):
            load_api_key(runner=runner)

    def test_empty_key_is_rejected(self) -> None:
        def runner(command: list[str], **_: object) -> subprocess.CompletedProcess[str]:
            return subprocess.CompletedProcess(command, 0, stdout=" \n", stderr="")

        with self.assertRaisesRegex(CredentialError, "empty"):
            load_api_key(runner=runner)


if __name__ == "__main__":
    unittest.main()
