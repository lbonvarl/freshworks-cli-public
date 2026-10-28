from __future__ import annotations

import argparse
import contextlib
import io
import json
import os
import unittest
from unittest import mock

from freshsales_cli.cli import _dispatch, build_parser, main
from freshsales_cli.errors import ApiError

# The CLI has no built-in API domain; give it a placeholder for these tests.
os.environ.setdefault(
    "FRESHSALES_BASE_URL", "https://example.myfreshworks.com/crm/sales/api"
)


class StubClient:
    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[object, ...], dict[str, object]]] = []

    def _result(self, name: str, *args: object, **kwargs: object) -> dict[str, object]:
        self.calls.append((name, args, kwargs))
        return {"operation": name, "args": list(args), "kwargs": kwargs}

    def get_contact(self, *args: object, **kwargs: object) -> dict[str, object]:
        return self._result("get_contact", *args, **kwargs)

    def contact_filters(self) -> dict[str, object]:
        return self._result("contact_filters")

    def list_contacts(self, *args: object, **kwargs: object) -> dict[str, object]:
        return self._result("list_contacts", *args, **kwargs)

    def get_account(self, *args: object, **kwargs: object) -> dict[str, object]:
        return self._result("get_account", *args, **kwargs)

    def account_filters(self) -> dict[str, object]:
        return self._result("account_filters")

    def list_accounts(self, *args: object, **kwargs: object) -> dict[str, object]:
        return self._result("list_accounts", *args, **kwargs)

    def get_deal(self, *args: object, **kwargs: object) -> dict[str, object]:
        return self._result("get_deal", *args, **kwargs)

    def deal_filters(self) -> dict[str, object]:
        return self._result("deal_filters")

    def list_deals(self, *args: object, **kwargs: object) -> dict[str, object]:
        return self._result("list_deals", *args, **kwargs)

    def search(self, *args: object, **kwargs: object) -> dict[str, object]:
        return self._result("search", *args, **kwargs)

    def lookup(self, *args: object, **kwargs: object) -> dict[str, object]:
        return self._result("lookup", *args, **kwargs)

    def campaigns(self) -> dict[str, object]:
        return self._result("campaigns")

    def list_tasks(self, *args: object, **kwargs: object) -> dict[str, object]:
        return self._result("list_tasks", *args, **kwargs)

    def get_task(self, *args: object, **kwargs: object) -> dict[str, object]:
        return self._result("get_task", *args, **kwargs)

    def list_appointments(self, *args: object, **kwargs: object) -> dict[str, object]:
        return self._result("list_appointments", *args, **kwargs)

    def get_appointment(self, *args: object, **kwargs: object) -> dict[str, object]:
        return self._result("get_appointment", *args, **kwargs)

    def fields(self, *args: object, **kwargs: object) -> dict[str, object]:
        return self._result("fields", *args, **kwargs)

    def selector(self, *args: object, **kwargs: object) -> dict[str, object]:
        return self._result("selector", *args, **kwargs)

    def marketing_lists(self, *args: object, **kwargs: object) -> dict[str, object]:
        return self._result("marketing_lists", *args, **kwargs)

    def marketing_list_contacts(
        self, *args: object, **kwargs: object
    ) -> dict[str, object]:
        self._result("marketing_list_contacts", *args, **kwargs)
        return {
            "contacts": [
                {"id": 1, "display_name": "Ana Lyst", "job_title": "Senior Analyst"},
                {"id": 2, "display_name": "Eng Ineer", "job_title": "Engineer"},
            ],
            "meta": {"total_pages": 1},
        }

    def contact_activities(self, *args: object, **kwargs: object) -> dict[str, object]:
        self._result("contact_activities", *args, **kwargs)
        return {
            "activities": [
                {
                    "action_type": "EMAIL_OPENED",
                    "created_at": "2026-09-01T10:00:00+00:00",
                    "action_data": {"campaign_name": "Product Launch"},
                }
            ]
        }


class CliTests(unittest.TestCase):
    def run_cli(self, argv: list[str], client: StubClient) -> tuple[int, str, str]:
        stdout = io.StringIO()
        stderr = io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            result = main(argv, client=client)
        return result, stdout.getvalue(), stderr.getvalue()

    def test_contact_get_is_json_and_does_not_load_keychain(self) -> None:
        client = StubClient()
        loader_called = False

        def loader() -> str:
            nonlocal loader_called
            loader_called = True
            return "must-not-be-called"

        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            result = main(
                ["contacts", "get", "42", "--include", "owner"],
                client=client,
                api_key_loader=loader,
            )

        self.assertEqual(result, 0)
        self.assertFalse(loader_called)
        self.assertIn('"operation": "get_contact"', stdout.getvalue())
        self.assertEqual(
            client.calls,
            [("get_contact", ("42",), {"include": "owner"})],
        )

    def test_every_entity_read_action_is_dispatched(self) -> None:
        client = StubClient()
        for argv, expected in [
            (["contacts", "filters"], ("contact_filters", (), {})),
            (
                [
                    "contacts",
                    "list",
                    "--view-id",
                    "4",
                    "--page",
                    "2",
                    "--sort",
                    "updated_at",
                    "--sort-type",
                    "desc",
                    "--include",
                    "owner",
                ],
                (
                    "list_contacts",
                    ("4",),
                    {
                        "page": 2,
                        "sort": "updated_at",
                        "sort_type": "desc",
                        "include": "owner",
                    },
                ),
            ),
            (
                ["accounts", "get", "5", "--include", "contacts"],
                ("get_account", ("5",), {"include": "contacts"}),
            ),
            (["accounts", "filters"], ("account_filters", (), {})),
            (["deals", "get", "6"], ("get_deal", ("6",), {"include": None})),
            (["deals", "filters"], ("deal_filters", (), {})),
            (
                ["deals", "list", "--view-id", "1", "--sort-type", "asc"],
                (
                    "list_deals",
                    ("1",),
                    {"page": 1, "sort": None, "sort_type": "asc", "include": None},
                ),
            ),
        ]:
            with self.subTest(argv=argv):
                code, stdout, stderr = self.run_cli(argv, client)
                self.assertEqual((code, stderr), (0, ""))
                self.assertEqual(client.calls[-1], expected)
                self.assertEqual(json.loads(stdout)["operation"], expected[0])

    def test_pretty_flag_indents_json(self) -> None:
        code, stdout, _ = self.run_cli(["--pretty", "deals", "filters"], StubClient())
        self.assertEqual(code, 0)
        self.assertTrue(stdout.startswith('{\n  "operation": "deal_filters"'))

    def test_numeric_options_are_validated_by_the_parser(self) -> None:
        for argv in [
            ["contacts", "activities", "1", "--limit", "0"],
            ["campaigns", "engagement", "--list-id", "1", "--max-pages", "0"],
            ["campaigns", "engagement", "--list-id", "1", "--delay", "-1"],
        ]:
            with (
                self.subTest(argv=argv),
                contextlib.redirect_stderr(io.StringIO()),
                self.assertRaises(SystemExit),
            ):
                main(argv, client=StubClient())
        code, _, stderr = self.run_cli(
            ["campaigns", "engagement", "--list-id", "1", "--delay", "0"], StubClient()
        )
        self.assertEqual((code, stderr), (0, ""))

    def test_list_search_and_lookup_arguments_are_dispatched(self) -> None:
        client = StubClient()
        self.assertEqual(
            self.run_cli(["accounts", "list", "--view-id", "7"], client)[0], 0
        )
        self.assertEqual(
            client.calls[-1],
            (
                "list_accounts",
                ("7",),
                {"page": 1, "sort": None, "sort_type": None, "include": None},
            ),
        )
        self.assertEqual(
            self.run_cli(
                ["search", "Acme", "--include", "contact,deal", "--per-page", "25"],
                client,
            )[0],
            0,
        )
        self.assertEqual(
            client.calls[-1],
            ("search", ("Acme",), {"include": "contact,deal", "per_page": 25}),
        )
        self.assertEqual(
            self.run_cli(
                [
                    "lookup",
                    "person@example.com",
                    "--field",
                    "email",
                    "--entities",
                    "contact",
                ],
                client,
            )[0],
            0,
        )
        self.assertEqual(
            client.calls[-1],
            (
                "lookup",
                ("person@example.com",),
                {"field": "email", "entities": "contact"},
            ),
        )

    def test_campaign_list_and_activity_commands_are_dispatched(self) -> None:
        client = StubClient()
        for argv, expected in [
            (["campaigns", "list"], ("campaigns", (), {})),
            (["selector", "owners"], ("selector", ("owners",), {})),
            (["lists", "list", "--page", "2"], ("marketing_lists", (), {"page": 2})),
            (
                ["lists", "contacts", "9"],
                ("marketing_list_contacts", ("9",), {"page": 1}),
            ),
            (
                ["contacts", "activities", "42", "--include", "user", "--limit", "5"],
                ("contact_activities", ("42",), {"include": "user", "limit": 5}),
            ),
        ]:
            with self.subTest(argv=argv):
                self.assertEqual(self.run_cli(argv, client)[0], 0)
                self.assertEqual(client.calls[-1], expected)

    def test_task_and_appointment_commands_are_dispatched(self) -> None:
        client = StubClient()
        for argv, expected in [
            (
                ["tasks", "list"],
                (
                    "list_tasks",
                    (),
                    {"filter_name": "open", "page": 1, "include": None},
                ),
            ),
            (
                [
                    "tasks",
                    "list",
                    "--filter",
                    "overdue",
                    "--page",
                    "2",
                    "--include",
                    "owner",
                ],
                (
                    "list_tasks",
                    (),
                    {"filter_name": "overdue", "page": 2, "include": "owner"},
                ),
            ),
            (["tasks", "get", "7"], ("get_task", ("7",), {"include": None})),
            (
                ["appointments", "list", "--filter", "past"],
                (
                    "list_appointments",
                    (),
                    {"filter_name": "past", "page": 1, "include": None},
                ),
            ),
            (
                ["appointments", "get", "8", "--include", "targetable"],
                ("get_appointment", ("8",), {"include": "targetable"}),
            ),
        ]:
            with self.subTest(argv=argv):
                self.assertEqual(self.run_cli(argv, client)[0], 0)
                self.assertEqual(client.calls[-1], expected)
        with (
            contextlib.redirect_stderr(io.StringIO()),
            self.assertRaises(SystemExit),
        ):
            main(["appointments", "list", "--filter", "today"], client=client)

    def test_selector_help_lists_the_known_tables(self) -> None:
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout), self.assertRaises(SystemExit):
            main(["selector", "--help"], client=StubClient())
        help_text = " ".join(stdout.getvalue().split())
        for name in ("owners", "deal_stages", "sales_activity_entity_types"):
            self.assertIn(name, help_text)

    def test_fields_command_is_dispatched_and_validates_the_entity(self) -> None:
        client = StubClient()
        for argv, expected in [
            (["fields", "contacts"], ("fields", ("contacts",), {"include": None})),
            (
                ["fields", "sales-activities", "--include", "field_group"],
                ("fields", ("sales-activities",), {"include": "field_group"}),
            ),
        ]:
            with self.subTest(argv=argv):
                self.assertEqual(self.run_cli(argv, client)[0], 0)
                self.assertEqual(client.calls[-1], expected)
        with (
            contextlib.redirect_stderr(io.StringIO()),
            self.assertRaises(SystemExit),
        ):
            main(["fields", "leads"], client=client)

    def test_campaign_engagement_filters_titles_and_outputs_csv(self) -> None:
        client = StubClient()
        code, stdout, stderr = self.run_cli(
            [
                "campaigns",
                "engagement",
                "--list-id",
                "9",
                "--campaign",
                "launch",
                "--title",
                "analyst",
                "--format",
                "csv",
            ],
            client,
        )
        self.assertEqual((code, stderr), (0, ""))
        lines = stdout.splitlines()
        self.assertTrue(lines[0].startswith("id,display_name,email,job_title"))
        self.assertEqual(len(lines), 2)
        self.assertIn("Ana Lyst", lines[1])
        self.assertIn("EMAIL_OPENED", lines[1])
        # Only the analyst's timeline is fetched.
        fetched = [c for c in client.calls if c[0] == "contact_activities"]
        self.assertEqual(fetched, [("contact_activities", (1,), {"limit": 100})])

    def test_activity_types_census_uses_small_default_limits(self) -> None:
        client = StubClient()
        code, stdout, stderr = self.run_cli(
            ["campaigns", "activity-types", "--list-id", "5"], client
        )
        self.assertEqual((code, stderr), (0, ""))
        report = json.loads(stdout)
        self.assertEqual(report["scanned_contacts"], 2)
        self.assertEqual(report["action_types"][0]["action_type"], "EMAIL_OPENED")
        self.assertNotIn("Product Launch", stdout)
        self.assertEqual(
            [c for c in client.calls if c[0] == "marketing_list_contacts"],
            [("marketing_list_contacts", ("5",), {"page": 1})],
        )

    def test_campaign_engagement_requires_one_audience(self) -> None:
        with contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                main(["campaigns", "engagement"], client=StubClient())
            with self.assertRaises(SystemExit):
                main(
                    ["campaigns", "engagement", "--list-id", "1", "--view-id", "2"],
                    client=StubClient(),
                )

    def test_invalid_engagement_filter_is_a_clean_error(self) -> None:
        code, stdout, stderr = self.run_cli(
            ["campaigns", "engagement", "--list-id", "1", "--match", "nofield"],
            StubClient(),
        )
        self.assertEqual((code, stdout), (1, ""))
        self.assertIn("FIELD=REGEX", stderr)

    def test_expected_application_error_goes_to_stderr(self) -> None:
        class FailingClient(StubClient):
            def get_contact(self, *args: object, **kwargs: object) -> dict[str, object]:
                raise ApiError(403, "forbidden")

        result, stdout, stderr = self.run_cli(["contacts", "get", "1"], FailingClient())
        self.assertEqual(result, 1)
        self.assertEqual(stdout, "")
        self.assertIn("403", stderr)
        self.assertIn("forbidden", stderr)

    def test_missing_base_url_is_a_clean_error_before_loading_the_key(self) -> None:
        def loader() -> str:
            raise AssertionError("the key must not be loaded without a base URL")

        stdout, stderr = io.StringIO(), io.StringIO()
        with (
            mock.patch.dict("os.environ", {"FRESHSALES_BASE_URL": ""}),
            contextlib.redirect_stdout(stdout),
            contextlib.redirect_stderr(stderr),
        ):
            code = main(["fields", "contacts"], api_key_loader=loader)
        self.assertEqual((code, stdout.getvalue()), (1, ""))
        self.assertIn("set FRESHSALES_BASE_URL or pass --base-url", stderr.getvalue())

    def test_base_url_comes_from_the_option_or_the_environment(self) -> None:
        with mock.patch.dict(
            "os.environ", {"FRESHSALES_BASE_URL": "https://env.example/api"}
        ):
            from_env = build_parser().parse_args(["fields", "contacts"])
            from_option = build_parser().parse_args(
                ["--base-url", "https://cli.example/api", *["fields", "contacts"]]
            )
        self.assertEqual(from_env.base_url, "https://env.example/api")
        self.assertEqual(from_option.base_url, "https://cli.example/api")

    def test_dispatch_rejects_an_unknown_command(self) -> None:
        # argparse makes this unreachable from the command line; the guard
        # keeps a future parser change from silently returning nothing.
        with self.assertRaisesRegex(ValueError, "unknown resource"):
            _dispatch(argparse.Namespace(resource="unknown"), StubClient())  # type: ignore[arg-type]


if __name__ == "__main__":
    unittest.main()
