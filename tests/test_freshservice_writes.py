from __future__ import annotations

import contextlib
import io
import json
import os
import unittest
from typing import Any
from unittest import mock

from freshservice_cli import writes
from freshservice_cli.cli import main
from freshservice_cli.client import FreshserviceClient, HttpResponse
from freshservice_cli.errors import ApiError, FreshserviceError, WriteDisabledError

# The CLI has no built-in API domain; give it a placeholder for these tests.
os.environ.setdefault(
    "FRESHSERVICE_BASE_URL", "https://example.freshservice.com/api/v2"
)


BASE = "https://example.test/api/v2"


class FakeWriteClient:
    """Answers the preflight GET and records sent requests."""

    base_url = BASE

    def __init__(self, *, ticket: dict[str, Any] | None = None) -> None:
        self.ticket = ticket
        self.sent: list[writes.WriteRequest] = []
        self.reads: list[object] = []
        self.read_kwargs: list[dict[str, object]] = []

    def get_ticket(self, ticket_id: object, **kwargs: object) -> dict[str, Any] | None:
        self.reads.append(ticket_id)
        self.read_kwargs.append(kwargs)
        return {"ticket": self.ticket} if self.ticket else None

    def send(self, request: writes.WriteRequest) -> dict[str, Any]:
        self.sent.append(request)
        return {"ticket": {"id": 77}}


def never_load_key() -> str:
    raise AssertionError("a dry run must not load the API key")


def run_cli(argv: list[str], **kwargs: Any) -> tuple[int, str, str]:
    stdout, stderr = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
        code = main(argv, **kwargs)
    return code, stdout.getvalue(), stderr.getvalue()


class TicketPlanTests(unittest.TestCase):
    def test_create_payload(self) -> None:
        request = writes.plan_create_ticket(
            subject=" [CLI-TEST] Printer ",
            description="Paper jam",
            workspace_id="4",
            requester_id="9",
            priority="2",
            status=3,
            tags=["clitest-1", "clitest-1", "smoke"],
        )
        self.assertEqual((request.method, request.path), ("POST", "/tickets"))
        self.assertEqual(
            request.body,
            {
                "subject": "[CLI-TEST] Printer",
                "description": "Paper jam",
                "workspace_id": 4,
                "priority": 2,
                "status": 3,
                "requester_id": 9,
                "tags": ["clitest-1", "smoke"],
            },
        )
        by_email = writes.plan_create_ticket(
            subject="s", description="d", workspace_id=2, email="a@example.com"
        )
        self.assertEqual(by_email.body["email"], "a@example.com")
        self.assertEqual((by_email.body["priority"], by_email.body["status"]), (1, 2))
        self.assertNotIn("tags", by_email.body)

    def test_update_and_note_payloads(self) -> None:
        update = writes.plan_update_ticket(
            "12", status="5", priority=4, subject="New", tags=[]
        )
        self.assertEqual((update.method, update.path), ("PUT", "/tickets/12"))
        self.assertEqual(
            update.body, {"subject": "New", "status": 5, "priority": 4, "tags": []}
        )
        note = writes.plan_add_note(12, " Checked ")
        self.assertEqual((note.method, note.path), ("POST", "/tickets/12/notes"))
        self.assertEqual(note.body, {"body": "Checked", "private": True})
        self.assertFalse(writes.plan_add_note(12, "x", private=False).body["private"])
        self.assertEqual(
            update.describe(BASE + "/")["url"], "https://example.test/api/v2/tickets/12"
        )

    def test_invalid_plans_are_rejected(self) -> None:
        base = {"subject": "s", "description": "d", "workspace_id": 2}
        for kwargs, message in [
            ({**base}, "exactly one"),
            ({**base, "requester_id": 1, "email": "a@b.c"}, "exactly one"),
            ({**base, "requester_id": 1, "priority": 5}, "priority must be one of"),
            ({**base, "requester_id": 1, "status": 1}, "status must be one of"),
            ({**base, "requester_id": "x"}, "requester id"),
            ({**base, "requester_id": 1, "subject": " "}, "subject must not be empty"),
            ({**base, "requester_id": 1, "subject": "s" * 256}, "at most 255"),
            ({**base, "requester_id": 1, "tags": ["t" * 33]}, "tag must be at most"),
            (
                {**base, "requester_id": 1, "tags": [f"t{i}" for i in range(11)]},
                "at most 10 tags",
            ),
        ]:
            with (
                self.subTest(kwargs=kwargs),
                self.assertRaisesRegex(ValueError, message),
            ):
                writes.plan_create_ticket(**kwargs)
        with self.assertRaisesRegex(ValueError, "nothing to update"):
            writes.plan_update_ticket(1)
        with self.assertRaisesRegex(ValueError, "ticket id"):
            writes.plan_update_ticket(0, status=2)
        with self.assertRaisesRegex(ValueError, "note must not be empty"):
            writes.plan_add_note(1, "  ")


class TicketRunTests(unittest.TestCase):
    def test_dry_run_sends_nothing_and_apply_needs_a_client(self) -> None:
        request = writes.plan_add_note(1, "x")
        report = writes.run(None, request, base_url=BASE, apply=False)
        self.assertTrue(report["dry_run"])
        self.assertNotIn("results", report)
        with self.assertRaisesRegex(ValueError, "client is required"):
            writes.run(None, request, base_url=BASE, apply=True)

    def test_apply_checks_the_ticket_then_sends(self) -> None:
        client = FakeWriteClient(
            ticket={
                "id": 1,
                "subject": "s",
                "status": 2,
                "priority": 1,
                "tags": ["vip"],
                "x": "y",
            }
        )
        request = writes.plan_update_ticket(1, status=5)
        report = writes.run(
            client,
            request,
            base_url=BASE,
            apply=True,
            preflight=lambda c: writes.ticket_summary(c, 1),
        )
        self.assertEqual(
            report["target"],
            {"id": 1, "subject": "s", "status": 2, "priority": 1, "tags": ["vip"]},
        )
        # Tags are requested so the report shows what `--tag` would replace.
        self.assertEqual(client.read_kwargs, [{"include": "tags"}])
        self.assertEqual(client.sent, [request])
        self.assertEqual(
            report["results"], [{"request": 0, "response": {"ticket": {"id": 77}}}]
        )

    def test_missing_ticket_blocks_the_write(self) -> None:
        client = FakeWriteClient(ticket=None)
        with self.assertRaisesRegex(FreshserviceError, "ticket 3 was not found"):
            writes.run(
                client,
                writes.plan_add_note(3, "x"),
                base_url=BASE,
                apply=True,
                preflight=lambda c: writes.ticket_summary(c, 3),
            )
        self.assertEqual(client.sent, [])


class TicketCliWriteTests(unittest.TestCase):
    def test_dry_runs_need_no_api_key(self) -> None:
        for argv, method, path in [
            (
                [
                    "tickets",
                    "create",
                    "--subject",
                    "S",
                    "--description",
                    "D",
                    "--workspace-id",
                    "4",
                    "--requester-id",
                    "9",
                    "--tag",
                    "t1",
                ],
                "POST",
                "/tickets",
            ),
            (["tickets", "update", "5", "--status", "5"], "PUT", "/tickets/5"),
            (["tickets", "add-note", "5", "--text", "hi"], "POST", "/tickets/5/notes"),
        ]:
            with self.subTest(argv=argv):
                code, stdout, stderr = run_cli(argv, api_key_loader=never_load_key)
                self.assertEqual((code, stderr), (0, ""))
                planned = json.loads(stdout)["requests"][0]
                self.assertEqual(planned["method"], method)
                self.assertEqual(
                    planned["url"], f"https://example.freshservice.com/api/v2{path}"
                )

    def test_apply_dispatches_each_command(self) -> None:
        ticket = {"id": 5, "subject": "s", "status": 2, "priority": 1}
        for argv, expected_body, expect_preflight in [
            (
                [
                    "tickets",
                    "create",
                    "--subject",
                    "S",
                    "--description",
                    "D",
                    "--workspace-id",
                    "4",
                    "--email",
                    "a@example.com",
                ],
                {
                    "subject": "S",
                    "description": "D",
                    "workspace_id": 4,
                    "priority": 1,
                    "status": 2,
                    "email": "a@example.com",
                },
                False,
            ),
            (
                ["tickets", "update", "5", "--tag", "a", "--tag", "b"],
                {"tags": ["a", "b"]},
                True,
            ),
            (
                ["tickets", "add-note", "5", "--text", "hi", "--public"],
                {"body": "hi", "private": False},
                True,
            ),
        ]:
            with self.subTest(argv=argv):
                client = FakeWriteClient(ticket=ticket)
                code, stdout, stderr = run_cli([*argv, "--apply"], client=client)
                self.assertEqual((code, stderr), (0, ""))
                self.assertEqual(client.sent[0].body, expected_body)
                self.assertEqual(bool(client.reads), expect_preflight)
                self.assertFalse(json.loads(stdout)["dry_run"])

    def test_only_apply_creates_a_write_enabled_client(self) -> None:
        with mock.patch("freshservice_cli.cli.FreshserviceClient") as factory:
            factory.return_value = FakeWriteClient(ticket={"id": 5})
            run_cli(
                ["tickets", "add-note", "5", "--text", "hi", "--apply"],
                api_key_loader=lambda: "key",
            )
            self.assertTrue(factory.call_args.kwargs["allow_writes"])
            run_cli(["tickets", "get", "5"], api_key_loader=lambda: "key")
            self.assertFalse(factory.call_args.kwargs["allow_writes"])
            factory.reset_mock()
            run_cli(
                ["tickets", "add-note", "5", "--text", "hi"],
                api_key_loader=never_load_key,
            )
            factory.assert_not_called()

    def test_invalid_input_and_api_errors_fail_cleanly(self) -> None:
        code, stdout, stderr = run_cli(
            ["tickets", "update", "5", "--priority", "9"],
            api_key_loader=never_load_key,
        )
        self.assertEqual((code, stdout), (1, ""))
        self.assertIn("priority must be one of", stderr)

        class RefusingClient(FakeWriteClient):
            def send(self, request: writes.WriteRequest) -> dict[str, Any]:
                raise ApiError(403, "forbidden")

        code, stdout, stderr = run_cli(
            ["tickets", "add-note", "5", "--text", "x", "--apply"],
            client=RefusingClient(ticket={"id": 5}),
        )
        self.assertEqual((code, stdout), (1, ""))
        self.assertIn("403", stderr)

    def test_create_requires_a_requester(self) -> None:
        with (
            contextlib.redirect_stderr(io.StringIO()),
            self.assertRaises(SystemExit),
        ):
            main(
                [
                    "tickets",
                    "create",
                    "--subject",
                    "S",
                    "--description",
                    "D",
                    "--workspace-id",
                    "4",
                ],
                api_key_loader=never_load_key,
            )


class ClientSendTests(unittest.TestCase):
    def test_send_is_blocked_unless_writes_are_enabled(self) -> None:
        transport = mock.Mock()
        transport.request.return_value = HttpResponse(201, b'{"ticket": {"id": 1}}', {})
        request = writes.plan_add_note(1, "x")
        blocked = FreshserviceClient("k", base_url=BASE, transport=transport)
        with self.assertRaises(WriteDisabledError):
            blocked.send(request)
        transport.request.assert_not_called()

        enabled = FreshserviceClient(
            "k", base_url=BASE, transport=transport, allow_writes=True
        )
        self.assertEqual(enabled.send(request), {"ticket": {"id": 1}})
        call = transport.request.call_args
        self.assertEqual(call.args, ("POST", f"{BASE}/tickets/1/notes"))
        self.assertEqual(
            json.loads(call.kwargs["body"]), {"body": "x", "private": True}
        )


if __name__ == "__main__":
    unittest.main()
