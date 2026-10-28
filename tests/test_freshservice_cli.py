from __future__ import annotations

import argparse
import base64
import contextlib
import io
import json
import os
import unittest
from unittest import mock

from freshservice_cli.cli import _dispatch, build_parser, main
from freshservice_cli.errors import ApiError

# The CLI has no built-in API domain; give it a placeholder for these tests.
os.environ.setdefault(
    "FRESHSERVICE_BASE_URL", "https://example.freshservice.com/api/v2"
)


class StubClient:
    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[object, ...], dict[str, object]]] = []

    def _result(self, name: str, *args: object, **kwargs: object) -> dict[str, object]:
        self.calls.append((name, args, kwargs))
        return {"operation": name, "args": list(args), "kwargs": kwargs}

    def get_ticket(self, *args: object, **kwargs: object) -> dict[str, object]:
        return self._result("get_ticket", *args, **kwargs)

    def list_tickets(self, *args: object, **kwargs: object) -> dict[str, object]:
        return self._result("list_tickets", *args, **kwargs)

    def filter_tickets(self, *args: object, **kwargs: object) -> dict[str, object]:
        return self._result("filter_tickets", *args, **kwargs)

    def ticket_conversations(
        self, *args: object, **kwargs: object
    ) -> dict[str, object]:
        return self._result("ticket_conversations", *args, **kwargs)

    def ticket_tasks(self, *args: object, **kwargs: object) -> dict[str, object]:
        return self._result("ticket_tasks", *args, **kwargs)

    def ticket_approvals(self, *args: object, **kwargs: object) -> dict[str, object]:
        return self._result("ticket_approvals", *args, **kwargs)

    def ticket_requested_items(
        self, *args: object, **kwargs: object
    ) -> dict[str, object]:
        return self._result("ticket_requested_items", *args, **kwargs)

    def ticket_fields(self, *args: object, **kwargs: object) -> dict[str, object]:
        return self._result("ticket_fields", *args, **kwargs)

    def get_requester(self, *args: object, **kwargs: object) -> dict[str, object]:
        return self._result("get_requester", *args, **kwargs)

    def list_requesters(self, *args: object, **kwargs: object) -> dict[str, object]:
        return self._result("list_requesters", *args, **kwargs)

    def get_agent(self, *args: object, **kwargs: object) -> dict[str, object]:
        return self._result("get_agent", *args, **kwargs)

    def list_agents(self, *args: object, **kwargs: object) -> dict[str, object]:
        return self._result("list_agents", *args, **kwargs)

    def get_current_agent(self, *args: object, **kwargs: object) -> dict[str, object]:
        return self._result("get_current_agent", *args, **kwargs)

    def get_department(self, *args: object, **kwargs: object) -> dict[str, object]:
        return self._result("get_department", *args, **kwargs)

    def list_departments(self, *args: object, **kwargs: object) -> dict[str, object]:
        return self._result("list_departments", *args, **kwargs)

    def get_group(self, *args: object, **kwargs: object) -> dict[str, object]:
        return self._result("get_group", *args, **kwargs)

    def list_groups(self, *args: object, **kwargs: object) -> dict[str, object]:
        return self._result("list_groups", *args, **kwargs)

    def list_requester_groups(
        self, *args: object, **kwargs: object
    ) -> dict[str, object]:
        return self._result("list_requester_groups", *args, **kwargs)

    def get_workspace(self, *args: object, **kwargs: object) -> dict[str, object]:
        return self._result("get_workspace", *args, **kwargs)

    def list_workspaces(self, *args: object, **kwargs: object) -> dict[str, object]:
        return self._result("list_workspaces", *args, **kwargs)

    def get_role(self, *args: object, **kwargs: object) -> dict[str, object]:
        return self._result("get_role", *args, **kwargs)

    def list_roles(self, *args: object, **kwargs: object) -> dict[str, object]:
        return self._result("list_roles", *args, **kwargs)

    def get_announcement(self, *args: object, **kwargs: object) -> dict[str, object]:
        return self._result("get_announcement", *args, **kwargs)

    def list_announcements(self, *args: object, **kwargs: object) -> dict[str, object]:
        return self._result("list_announcements", *args, **kwargs)

    def get_canned_response(self, *args: object, **kwargs: object) -> dict[str, object]:
        return self._result("get_canned_response", *args, **kwargs)

    def list_canned_responses(
        self, *args: object, **kwargs: object
    ) -> dict[str, object]:
        return self._result("list_canned_responses", *args, **kwargs)

    def list_canned_response_folders(
        self, *args: object, **kwargs: object
    ) -> dict[str, object]:
        return self._result("list_canned_response_folders", *args, **kwargs)

    def list_solution_categories(
        self, *args: object, **kwargs: object
    ) -> dict[str, object]:
        return self._result("list_solution_categories", *args, **kwargs)

    def get_solution_category(
        self, *args: object, **kwargs: object
    ) -> dict[str, object]:
        return self._result("get_solution_category", *args, **kwargs)

    def list_solution_folders(
        self, *args: object, **kwargs: object
    ) -> dict[str, object]:
        return self._result("list_solution_folders", *args, **kwargs)

    def get_solution_folder(self, *args: object, **kwargs: object) -> dict[str, object]:
        return self._result("get_solution_folder", *args, **kwargs)

    def list_solution_articles(
        self, *args: object, **kwargs: object
    ) -> dict[str, object]:
        return self._result("list_solution_articles", *args, **kwargs)

    def get_solution_article(
        self, *args: object, **kwargs: object
    ) -> dict[str, object]:
        return self._result("get_solution_article", *args, **kwargs)

    def search_solution_articles(
        self, *args: object, **kwargs: object
    ) -> dict[str, object]:
        return self._result("search_solution_articles", *args, **kwargs)


class FreshserviceCliTests(unittest.TestCase):
    def run_cli(self, argv: list[str], client: StubClient) -> tuple[int, str, str]:
        stdout = io.StringIO()
        stderr = io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            result = main(argv, client=client)
        return result, stdout.getvalue(), stderr.getvalue()

    def test_ticket_get_does_not_load_keychain(self) -> None:
        client = StubClient()
        loader_called = False

        def loader() -> str:
            nonlocal loader_called
            loader_called = True
            return "must-not-be-called"

        result, stdout, stderr = self.run_cli(
            ["--pretty", "tickets", "get", "42", "--include", "requester"],
            client,
        )
        self.assertEqual(result, 0)
        self.assertFalse(loader_called)
        self.assertEqual(stderr, "")
        self.assertIn('"operation": "get_ticket"', stdout)
        self.assertEqual(
            client.calls,
            [("get_ticket", ("42",), {"include": "requester"})],
        )

    def test_list_and_filter_arguments_are_dispatched(self) -> None:
        client = StubClient()
        result, _, _ = self.run_cli(
            [
                "tickets",
                "list",
                "--page",
                "2",
                "--per-page",
                "50",
                "--include",
                "requester",
                "--order-type",
                "asc",
                "--filter",
                "watching",
                "--requester-id",
                "9",
                "--email",
                "person@example.com",
                "--updated-since",
                "2026-01-01T00:00:00Z",
                "--type",
                "Incident",
                "--workspace-id",
                "3",
            ],
            client,
        )
        self.assertEqual(result, 0)
        self.assertEqual(
            client.calls[-1],
            (
                "list_tickets",
                (),
                {
                    "page": 2,
                    "per_page": 50,
                    "include": "requester",
                    "order_type": "asc",
                    "filter_name": "watching",
                    "requester_id": "9",
                    "email": "person@example.com",
                    "updated_since": "2026-01-01T00:00:00Z",
                    "ticket_type": "Incident",
                    "workspace_id": "3",
                },
            ),
        )

        result, _, _ = self.run_cli(
            ["filter-tickets", "priority:3 AND status:2", "--page", "3"],
            client,
        )
        self.assertEqual(result, 0)
        self.assertEqual(
            client.calls[-1],
            (
                "filter_tickets",
                ("priority:3 AND status:2",),
                {"page": 3, "per_page": 30, "workspace_id": None},
            ),
        )

    def test_requester_alias_and_other_entities_are_dispatched(self) -> None:
        client = StubClient()
        self.assertEqual(
            self.run_cli(
                [
                    "contacts",
                    "list",
                    "--page",
                    "2",
                    "--email",
                    "person@example.com",
                    "--include-agents",
                ],
                client,
            )[0],
            0,
        )
        self.assertEqual(
            client.calls[-1],
            (
                "list_requesters",
                (),
                {
                    "page": 2,
                    "per_page": 30,
                    "email": "person@example.com",
                    "mobile_phone_number": None,
                    "work_phone_number": None,
                    "include_agents": True,
                },
            ),
        )

        self.assertEqual(self.run_cli(["agents", "get", "3"], client)[0], 0)
        self.assertEqual(client.calls[-1], ("get_agent", ("3",), {}))
        self.assertEqual(self.run_cli(["agents", "me"], client)[0], 0)
        self.assertEqual(client.calls[-1], ("get_current_agent", (), {}))
        # `companies` is kept as an alias: Freshservice has no /companies route.
        for resource in ("departments", "companies"):
            self.assertEqual(
                self.run_cli([resource, "list", "--query", "Acme"], client)[0], 0
            )
            self.assertEqual(
                client.calls[-1],
                (
                    "list_departments",
                    (),
                    {"page": 1, "per_page": 30, "query": "Acme", "workspace_id": None},
                ),
            )

    def test_every_detail_action_is_dispatched(self) -> None:
        client = StubClient()
        for argv, expected in [
            (["requesters", "get", "4"], ("get_requester", ("4",), {})),
            (["contacts", "get", "4"], ("get_requester", ("4",), {})),
            (["departments", "get", "5"], ("get_department", ("5",), {})),
            (["companies", "get", "5"], ("get_department", ("5",), {})),
            (
                ["agents", "list", "--query", "name:'Ada'", "--workspace-id", "2"],
                (
                    "list_agents",
                    (),
                    {
                        "page": 1,
                        "per_page": 30,
                        "query": "name:'Ada'",
                        "workspace_id": "2",
                    },
                ),
            ),
        ]:
            with self.subTest(argv=argv):
                code, stdout, stderr = self.run_cli(argv, client)
                self.assertEqual((code, stderr), (0, ""))
                self.assertEqual(client.calls[-1], expected)
                self.assertEqual(json.loads(stdout)["operation"], expected[0])

    def test_ticket_sub_resources_are_dispatched(self) -> None:
        client = StubClient()
        for argv, expected in [
            (
                ["tickets", "conversations", "42"],
                ("ticket_conversations", ("42",), {"page": 1, "per_page": 30}),
            ),
            (
                ["tickets", "conversations", "42", "--page", "2", "--per-page", "100"],
                ("ticket_conversations", ("42",), {"page": 2, "per_page": 100}),
            ),
            (
                ["tickets", "tasks", "42", "--per-page", "5"],
                ("ticket_tasks", ("42",), {"page": 1, "per_page": 5}),
            ),
            (["tickets", "approvals", "42"], ("ticket_approvals", ("42",), {})),
            (
                ["tickets", "requested-items", "42"],
                ("ticket_requested_items", ("42",), {}),
            ),
        ]:
            with self.subTest(argv=argv):
                code, _, stderr = self.run_cli(argv, client)
                self.assertEqual((code, stderr), (0, ""))
                self.assertEqual(client.calls[-1], expected)

    def test_directory_and_knowledge_base_commands_are_dispatched(self) -> None:
        client = StubClient()
        paged = {"page": 1, "per_page": 30}
        for argv, expected in [
            (["groups", "list"], ("list_groups", (), paged)),
            (["groups", "get", "1"], ("get_group", ("1",), {})),
            (
                ["requester-groups", "list", "--per-page", "100"],
                ("list_requester_groups", (), {"page": 1, "per_page": 100}),
            ),
            (["workspaces", "list"], ("list_workspaces", (), paged)),
            (["workspaces", "get", "2"], ("get_workspace", ("2",), {})),
            (
                ["roles", "list", "--page", "2"],
                ("list_roles", (), {"page": 2, "per_page": 30}),
            ),
            (["roles", "get", "3"], ("get_role", ("3",), {})),
            (
                ["announcements", "list", "--state", "archived"],
                ("list_announcements", (), {**paged, "state": "archived"}),
            ),
            (
                ["announcements", "list"],
                ("list_announcements", (), {**paged, "state": None}),
            ),
            (["announcements", "get", "4"], ("get_announcement", ("4",), {})),
            (["canned-responses", "list"], ("list_canned_responses", (), paged)),
            (
                ["canned-responses", "get", "5"],
                ("get_canned_response", ("5",), {}),
            ),
            (
                ["canned-responses", "folders"],
                ("list_canned_response_folders", (), {}),
            ),
            (
                ["solutions", "categories", "--workspace-id", "2"],
                ("list_solution_categories", (), {**paged, "workspace_id": "2"}),
            ),
            (
                ["solutions", "folders", "--category-id", "6"],
                ("list_solution_folders", ("6",), paged),
            ),
            (
                ["solutions", "articles", "--folder-id", "7", "--per-page", "100"],
                ("list_solution_articles", ("7",), {"page": 1, "per_page": 100}),
            ),
            (["solutions", "category", "6"], ("get_solution_category", ("6",), {})),
            (["solutions", "folder", "7"], ("get_solution_folder", ("7",), {})),
            (["solutions", "article", "8"], ("get_solution_article", ("8",), {})),
            (
                ["solutions", "search", "vpn"],
                ("search_solution_articles", ("vpn",), paged),
            ),
        ]:
            with self.subTest(argv=argv):
                code, _, stderr = self.run_cli(argv, client)
                self.assertEqual((code, stderr), (0, ""))
                self.assertEqual(client.calls[-1], expected)

    def test_unavailable_options_are_rejected_by_the_parser(self) -> None:
        for argv in [
            ["requester-groups", "get", "1"],
            ["announcements", "list", "--state", "draft"],
            ["solutions", "folders"],
            ["groups", "list", "--workspace-id", "2"],
        ]:
            with (
                self.subTest(argv=argv),
                contextlib.redirect_stderr(io.StringIO()),
                self.assertRaises(SystemExit),
            ):
                main(argv, client=StubClient())

    def test_ticket_fields_is_dispatched(self) -> None:
        client = StubClient()
        for argv, expected in [
            (["ticket-fields"], ("ticket_fields", (), {"workspace_id": None})),
            (
                ["ticket-fields", "--workspace-id", "2"],
                ("ticket_fields", (), {"workspace_id": "2"}),
            ),
        ]:
            with self.subTest(argv=argv):
                self.assertEqual(self.run_cli(argv, client)[0], 0)
                self.assertEqual(client.calls[-1], expected)

    def test_default_client_uses_the_loaded_key_and_stays_read_only(self) -> None:
        response = mock.MagicMock()
        response.getcode.return_value = 200
        response.read.return_value = b'{"agent": {"id": 3}}'
        response.headers.items.return_value = []
        response.__enter__.return_value = response
        stdout = io.StringIO()
        with (
            mock.patch(
                "freshservice_cli.client.urlopen", return_value=response
            ) as urlopen,
            contextlib.redirect_stdout(stdout),
        ):
            code = main(
                ["--base-url", "https://example.test/api/v2", "agents", "get", "3"],
                api_key_loader=lambda: "test-api-key",
            )
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(stdout.getvalue()), {"agent": {"id": 3}})
        request = urlopen.call_args.args[0]
        self.assertEqual(request.get_method(), "GET")
        self.assertEqual(request.full_url, "https://example.test/api/v2/agents/3")
        token = base64.b64encode(b"test-api-key:X").decode("ascii")
        self.assertEqual(request.get_header("Authorization"), f"Basic {token}")

    def test_expected_api_error_is_written_to_stderr(self) -> None:
        class FailingClient(StubClient):
            def get_ticket(self, *args: object, **kwargs: object) -> dict[str, object]:
                raise ApiError(403, "forbidden")

        result, stdout, stderr = self.run_cli(["tickets", "get", "1"], FailingClient())
        self.assertEqual(result, 1)
        self.assertEqual(stdout, "")
        self.assertIn("403", stderr)
        self.assertIn("forbidden", stderr)

    def test_missing_base_url_is_a_clean_error_before_loading_the_key(self) -> None:
        def loader() -> str:
            raise AssertionError("the key must not be loaded without a base URL")

        stdout, stderr = io.StringIO(), io.StringIO()
        with (
            mock.patch.dict("os.environ", {"FRESHSERVICE_BASE_URL": ""}),
            contextlib.redirect_stdout(stdout),
            contextlib.redirect_stderr(stderr),
        ):
            code = main(["ticket-fields"], api_key_loader=loader)
        self.assertEqual((code, stdout.getvalue()), (1, ""))
        self.assertIn("set FRESHSERVICE_BASE_URL or pass --base-url", stderr.getvalue())

    def test_base_url_comes_from_the_option_or_the_environment(self) -> None:
        with mock.patch.dict(
            "os.environ", {"FRESHSERVICE_BASE_URL": "https://env.example/api"}
        ):
            from_env = build_parser().parse_args(["ticket-fields"])
            from_option = build_parser().parse_args(
                ["--base-url", "https://cli.example/api", *["ticket-fields"]]
            )
        self.assertEqual(from_env.base_url, "https://env.example/api")
        self.assertEqual(from_option.base_url, "https://cli.example/api")

    def test_dispatch_rejects_an_unknown_command(self) -> None:
        # argparse makes this unreachable from the command line; the guard
        # keeps a future parser change from silently returning nothing.
        with self.assertRaisesRegex(ValueError, "unknown resource"):
            _dispatch(
                argparse.Namespace(resource="tickets", action="unknown"), StubClient()
            )  # type: ignore[arg-type]


if __name__ == "__main__":
    unittest.main()
