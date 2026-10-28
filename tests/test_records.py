from __future__ import annotations

import contextlib
import io
import json
import os
import unittest
from typing import Any
from unittest import mock

from freshsales_cli import records
from freshsales_cli.cli import main
from freshsales_cli.errors import ApiError, FreshsalesError

# The CLI has no built-in API domain; give it a placeholder for these tests.
os.environ.setdefault(
    "FRESHSALES_BASE_URL", "https://example.myfreshworks.com/crm/sales/api"
)


BASE = "https://example.myfreshworks.com/crm/sales/api"


class FakeRecordClient:
    """Answers preflight GETs from a dict and records sent requests."""

    base_url = BASE

    def __init__(
        self,
        existing: dict[tuple[str, str], dict[str, Any]] | None = None,
        *,
        lookup_result: Any = None,
    ) -> None:
        self.existing = existing or {}
        self.lookup_result = lookup_result
        self.sent: list[Any] = []
        self.reads: list[tuple[str, str]] = []

    def _get(self, method: str, record_id: object) -> dict[str, Any]:
        key = (method, str(record_id))
        self.reads.append(key)
        if key not in self.existing:
            raise ApiError(404, "not found")
        return self.existing[key]

    def get_contact(self, record_id: object, **_: object) -> dict[str, Any]:
        return self._get("get_contact", record_id)

    def get_account(self, record_id: object, **_: object) -> dict[str, Any]:
        return self._get("get_account", record_id)

    def get_deal(self, record_id: object, **_: object) -> dict[str, Any]:
        return self._get("get_deal", record_id)

    def get_task(self, record_id: object, **_: object) -> dict[str, Any]:
        return self._get("get_task", record_id)

    def lookup(self, *args: object, **kwargs: object) -> Any:
        self.reads.append(("lookup", str(args[0])))
        return self.lookup_result

    def send(self, request: Any) -> dict[str, Any]:
        self.sent.append(request)
        return {"ok": True}


def never_load_key() -> str:
    raise AssertionError("a dry run must not load the API key")


def run_cli(argv: list[str], **kwargs: Any) -> tuple[int, str, str]:
    stdout, stderr = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
        code = main(argv, **kwargs)
    return code, stdout.getvalue(), stderr.getvalue()


class AssignmentTests(unittest.TestCase):
    def test_parse_assignments(self) -> None:
        self.assertEqual(
            records.parse_assignments(["name = Acme", "website=a=b", "city="]),
            {"name": "Acme", "website": "a=b", "city": ""},
        )
        for items, message in [
            (["name"], "FIELD=VALUE"),
            (["=x"], "FIELD=VALUE"),
            (["a=1", "a=2"], "more than once"),
        ]:
            with self.subTest(items=items), self.assertRaisesRegex(ValueError, message):
                records.parse_assignments(items)


class PlanTests(unittest.TestCase):
    def test_create_converts_each_field_kind(self) -> None:
        request = records.plan_create(
            "deals",
            {
                "name": "[CLI-TEST] Deal",
                "amount": "1500.50",
                "sales_account_id": "7",
                "expected_close": "2026-12-31",
                "cf_screening_check": "False",
                "cf_region": "EMEA",
            },
            ["clitest-1", "clitest-1"],
        )
        self.assertEqual((request.method, request.path), ("POST", "/deals"))
        self.assertEqual(
            request.body,
            {
                "deal": {
                    "name": "[CLI-TEST] Deal",
                    "amount": 1500.5,
                    "sales_account_id": 7,
                    "expected_close": "2026-12-31",
                    "custom_field": {
                        "cf_screening_check": False,
                        "cf_region": "EMEA",
                    },
                    "tags": ["clitest-1"],
                }
            },
        )
        self.assertEqual(request.summary, "create deal '[CLI-TEST] Deal'")

        contact = records.plan_create(
            "contacts",
            {"email": "t@example.com", "subscription_status": "0", "owner_id": "3"},
        )
        self.assertEqual(
            contact.body,
            {
                "contact": {
                    "email": "t@example.com",
                    "subscription_status": 0,
                    "owner_id": 3,
                }
            },
        )
        self.assertEqual(contact.summary, "create contact 'contact'")
        self.assertEqual(
            records.plan_create("deals", {"name": "d", "amount": "2"}).body["deal"][
                "amount"
            ],
            2,
        )

    def test_task_and_note_payloads(self) -> None:
        task = records.plan_create(
            "tasks",
            {
                "title": "Call",
                "due_date": "2026-11-01T10:00:00Z",
                "targetable_type": "Contact",
                "targetable_id": "5",
            },
        )
        self.assertEqual(task.path, "/tasks")
        self.assertEqual(task.body["task"]["targetable_type"], "Contact")
        note = records.plan_create(
            "notes",
            {"description": "Hi", "targetable_type": "Deal", "targetable_id": "9"},
        )
        self.assertEqual(
            note.body,
            {
                "note": {
                    "description": "Hi",
                    "targetable_type": "Deal",
                    "targetable_id": 9,
                }
            },
        )

    def test_update_payload_and_summary(self) -> None:
        request = records.plan_update(
            "accounts", " 12 ", {"city": "Paris", "cf_contract_status": "Active"}, ["a"]
        )
        self.assertEqual((request.method, request.path), ("PUT", "/sales_accounts/12"))
        self.assertEqual(
            request.body,
            {
                "sales_account": {
                    "city": "Paris",
                    "custom_field": {"cf_contract_status": "Active"},
                    "tags": ["a"],
                }
            },
        )
        self.assertEqual(
            request.summary, "update cf_contract_status, city, tags of account 12"
        )
        self.assertEqual(
            records.plan_update("tasks", 3, {"status": "1"}).body,
            {"task": {"status": 1}},
        )
        self.assertEqual(
            records.plan_update("contacts", 3, {}, []).body, {"contact": {"tags": []}}
        )

    def test_invalid_plans_are_rejected(self) -> None:
        cases: list[tuple[Any, str]] = [
            (lambda: records.plan_create("leads", {"name": "x"}), "cannot write"),
            (lambda: records.plan_create("accounts", {"city": "x"}), "requires: name"),
            (lambda: records.plan_create("contacts", {}), "nothing to create"),
            (
                lambda: records.plan_create("contacts", {"mobile_number": "1"}),
                "not writable; allowed: .* and custom cf_",
            ),
            (
                lambda: records.plan_create("notes", {"cf_x": "1"}),
                "not writable; allowed: description, targetable_type, targetable_id$",
            ),
            (lambda: records.plan_create("deals", {"name": ""}), "must not be empty"),
            (
                lambda: records.plan_create("deals", {"name": "x" * 10001}),
                "at most 10000",
            ),
            (
                lambda: records.plan_create("contacts", {"email": "nope"}),
                "email address",
            ),
            (
                lambda: records.plan_create("deals", {"name": "d", "owner_id": "0"}),
                "positive integer",
            ),
            (
                lambda: records.plan_create("contacts", {"subscription_status": "-1"}),
                "non-negative integer",
            ),
            (
                lambda: records.plan_create("deals", {"name": "d", "amount": "lots"}),
                "must be a number",
            ),
            (
                lambda: records.plan_create("deals", {"name": "d", "amount": "inf"}),
                "finite number",
            ),
            (
                lambda: records.plan_create(
                    "deals", {"name": "d", "expected_close": "31/12/2026"}
                ),
                "YYYY-MM-DD",
            ),
            (
                lambda: records.plan_update("tasks", 1, {"due_date": "tomorrow"}),
                "ISO date",
            ),
            (
                lambda: records.plan_create(
                    "notes",
                    {
                        "description": "d",
                        "targetable_type": "Lead",
                        "targetable_id": "1",
                    },
                ),
                "targetable_type must be one of",
            ),
            (
                lambda: records.plan_update("tasks", 1, {"targetable_id": "2"}),
                "cannot be changed",
            ),
            (
                lambda: records.plan_update("notes", 1, {"description": "x"}),
                "cannot be updated",
            ),
            (lambda: records.plan_update("deals", "x", {"name": "d"}), "record id"),
            (lambda: records.plan_update("deals", 1, {}), "nothing to update"),
            (
                lambda: records.plan_create(
                    "tasks",
                    {
                        "title": "t",
                        "due_date": "2026-11-01",
                        "targetable_type": "Deal",
                        "targetable_id": "1",
                    },
                    ["x"],
                ),
                "have no tags",
            ),
            (
                lambda: records.plan_update("deals", 1, {}, [" "]),
                "tag must not be empty",
            ),
            (lambda: records.plan_update("deals", 1, {}, ["t" * 65]), "at most 64"),
            (
                lambda: records.plan_update(
                    "deals", 1, {}, [f"t{i}" for i in range(11)]
                ),
                "at most 10 tags",
            ),
        ]
        for build, message in cases:
            with (
                self.subTest(message=message),
                self.assertRaisesRegex(ValueError, message),
            ):
                build()


class PreflightTests(unittest.TestCase):
    def test_update_target_reports_current_values(self) -> None:
        client = FakeRecordClient(
            {
                ("get_deal", "4"): {
                    "deal": {
                        "id": 4,
                        "amount": "10.0",
                        "custom_field": {"cf_region": "APAC"},
                    }
                }
            }
        )
        request = records.plan_update(
            "deals", 4, {"amount": "20", "cf_region": "EMEA"}, ["t"]
        )
        self.assertEqual(
            records.check_update_target(client, "deals", 4, request),
            {"id": 4, "current": {"amount": "10.0", "cf_region": "APAC", "tags": None}},
        )

    def test_missing_or_malformed_update_target_is_refused(self) -> None:
        request = records.plan_update("contacts", 4, {"city": "x"})
        for existing, message in [
            ({}, "contact 4 was not found"),
            ({("get_contact", "4"): {}}, "contact 4 was not found"),
            ({("get_contact", "4"): {"other": 1}}, "contact 4 was not found"),
        ]:
            with (
                self.subTest(existing=existing),
                self.assertRaisesRegex(FreshsalesError, message),
            ):
                records.check_update_target(
                    FakeRecordClient(existing), "contacts", 4, request
                )

    def test_non_404_errors_are_not_masked(self) -> None:
        class Forbidden(FakeRecordClient):
            def get_task(self, record_id: object, **_: object) -> dict[str, Any]:
                raise ApiError(403, "forbidden")

        request = records.plan_update("tasks", 1, {"status": "1"})
        with self.assertRaisesRegex(ApiError, "403"):
            records.check_update_target(Forbidden(), "tasks", 1, request)

    def test_create_references_are_checked(self) -> None:
        client = FakeRecordClient(
            {
                ("get_account", "7"): {"sales_account": {"id": 7}},
                ("get_contact", "5"): {"contact": {"id": 5}},
            },
            lookup_result={"contacts": {"contacts": []}},
        )
        deal = records.plan_create("deals", {"name": "d", "sales_account_id": "7"})
        self.assertEqual(
            records.check_create_references(client, deal), {"sales_account_id": 7}
        )
        task = records.plan_create(
            "tasks",
            {
                "title": "t",
                "due_date": "2026-11-01",
                "targetable_type": "Contact",
                "targetable_id": "5",
            },
        )
        self.assertEqual(
            records.check_create_references(client, task), {"targetable": "Contact 5"}
        )
        contact = records.plan_create("contacts", {"email": "new@example.com"})
        self.assertEqual(
            records.check_create_references(client, contact), {"email_is_new": True}
        )
        self.assertEqual(
            records.check_create_references(
                client, records.plan_create("accounts", {"name": "a"})
            ),
            {},
        )

        missing = records.plan_create("deals", {"name": "d", "sales_account_id": "8"})
        with self.assertRaisesRegex(FreshsalesError, "account 8 was not found"):
            records.check_create_references(client, missing)

    def test_existing_contact_email_blocks_a_create(self) -> None:
        request = records.plan_create("contacts", {"email": "taken@example.com"})
        for found in (
            {"contacts": {"contacts": [{"id": 1}]}},
            {"contacts": [{"id": 1}]},
        ):
            with (
                self.subTest(found=found),
                self.assertRaisesRegex(ValueError, "already exists"),
            ):
                records.check_create_references(
                    FakeRecordClient(lookup_result=found), request
                )
        for found in (None, [], {"contacts": None}):
            with self.subTest(found=found):
                self.assertTrue(
                    records.check_create_references(
                        FakeRecordClient(lookup_result=found), request
                    )["email_is_new"]
                )


class CliRecordWriteTests(unittest.TestCase):
    def test_dry_runs_need_no_api_key(self) -> None:
        for argv, method, path in [
            (["contacts", "create", "--set", "last_name=T"], "POST", "/contacts"),
            (["accounts", "update", "3", "--tag", "x"], "PUT", "/sales_accounts/3"),
            (["deals", "update", "4", "--set", "amount=5"], "PUT", "/deals/4"),
            (["tasks", "update", "6", "--set", "status=1"], "PUT", "/tasks/6"),
            (
                [
                    "notes",
                    "create",
                    "--set",
                    "description=x",
                    "--set",
                    "targetable_type=Deal",
                    "--set",
                    "targetable_id=4",
                ],
                "POST",
                "/notes",
            ),
        ]:
            with self.subTest(argv=argv):
                code, stdout, stderr = run_cli(argv, api_key_loader=never_load_key)
                self.assertEqual((code, stderr), (0, ""))
                planned = json.loads(stdout)["requests"][0]
                self.assertEqual(
                    (planned["method"], planned["url"]), (method, BASE + path)
                )

    def test_apply_preflights_then_sends(self) -> None:
        client = FakeRecordClient(
            {("get_deal", "4"): {"deal": {"id": 4, "name": "Old"}}}
        )
        code, stdout, stderr = run_cli(
            ["deals", "update", "4", "--set", "name=New", "--apply"], client=client
        )
        self.assertEqual((code, stderr), (0, ""))
        report = json.loads(stdout)
        self.assertEqual(report["targets"], {"id": 4, "current": {"name": "Old"}})
        self.assertEqual(client.sent[0].body, {"deal": {"name": "New"}})

        client = FakeRecordClient()
        code, stdout, stderr = run_cli(
            [
                "accounts",
                "create",
                "--set",
                "name=[CLI-TEST] A",
                "--tag",
                "t",
                "--apply",
            ],
            client=client,
        )
        self.assertEqual((code, stderr), (0, ""))
        self.assertEqual(client.sent[0].path, "/sales_accounts")

    def test_failed_preflight_sends_nothing(self) -> None:
        client = FakeRecordClient()
        code, stdout, stderr = run_cli(
            ["tasks", "update", "6", "--set", "status=1", "--apply"], client=client
        )
        self.assertEqual((code, stdout), (1, ""))
        self.assertIn("task 6 was not found", stderr)
        self.assertEqual(client.sent, [])

    def test_only_apply_creates_a_write_enabled_client(self) -> None:
        with mock.patch("freshsales_cli.cli.FreshsalesClient") as factory:
            factory.return_value = FakeRecordClient(
                {("get_contact", "2"): {"contact": {"id": 2}}}
            )
            run_cli(
                ["contacts", "update", "2", "--set", "city=Lyon", "--apply"],
                api_key_loader=lambda: "key",
            )
            self.assertTrue(factory.call_args.kwargs["allow_writes"])
            factory.reset_mock()
            run_cli(
                ["contacts", "update", "2", "--set", "city=Lyon"],
                api_key_loader=never_load_key,
            )
            factory.assert_not_called()

    def test_notes_have_no_update_and_tasks_no_tags(self) -> None:
        for argv in (
            ["notes", "update", "1", "--set", "description=x"],
            ["tasks", "create", "--tag", "x"],
        ):
            with (
                self.subTest(argv=argv),
                contextlib.redirect_stderr(io.StringIO()),
                self.assertRaises(SystemExit),
            ):
                main(argv, api_key_loader=never_load_key)


if __name__ == "__main__":
    unittest.main()
