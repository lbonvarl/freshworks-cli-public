from __future__ import annotations

import contextlib
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from freshsales_cli import writes
from freshsales_cli.cli import main
from freshsales_cli.client import FreshsalesClient, HttpResponse, WriteRequest
from freshsales_cli.errors import (
    ApiError,
    CredentialError,
    FreshsalesError,
    WriteDisabledError,
)

# The CLI has no built-in API domain; give it a placeholder for these tests.
os.environ.setdefault(
    "FRESHSALES_BASE_URL", "https://example.myfreshworks.com/crm/sales/api"
)

BASE = "https://example.test/crm/sales/api"


class RecordingTransport:
    def __init__(self) -> None:
        self.requests: list[dict[str, Any]] = []

    def request(self, method: str, url: str, *, headers, body, timeout):
        self.requests.append({"method": method, "url": url, "body": body})
        return HttpResponse(200, b'{"ok": true}', {})


def make_client(
    transport: RecordingTransport, *, allow_writes: bool = False
) -> FreshsalesClient:
    return FreshsalesClient(
        "test-api-key", base_url=BASE, transport=transport, allow_writes=allow_writes
    )


class FakeWriteClient:
    """Marketing lists in memory; records every write that is sent."""

    base_url = BASE

    def __init__(
        self,
        lists: list[dict[str, Any]] | None = None,
        *,
        fail_on: int | None = None,
    ) -> None:
        self.lists = (
            lists
            if lists is not None
            else [
                {"id": 1, "name": "Product Launch"},
                {"id": 2, "name": "Analysts"},
            ]
        )
        self.fail_on = fail_on
        self.sent: list[WriteRequest] = []

    def marketing_lists(self, *, page: int = 1) -> Any:
        return {"lists": self.lists if page == 1 else [], "meta": {"total_pages": 1}}

    def send(self, request: WriteRequest) -> Any:
        if self.fail_on is not None and len(self.sent) == self.fail_on:
            raise ApiError(429, "rate limited")
        self.sent.append(request)
        return {"message": "ok"}


def run_cli(argv: list[str], **kwargs: Any) -> tuple[int, str, str]:
    stdout, stderr = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
        code = main(argv, **kwargs)
    return code, stdout.getvalue(), stderr.getvalue()


def never_load_key() -> str:
    raise AssertionError("the API key must not be loaded")


class PlanTests(unittest.TestCase):
    def test_payloads_match_the_documented_api(self) -> None:
        plans = [
            FreshsalesClient.plan_create_list("  Analysts  "),
            FreshsalesClient.plan_rename_list("7", "Analysts 2026"),
            FreshsalesClient.plan_add_contacts(7, [3, "4", 3]),
            FreshsalesClient.plan_remove_contacts(7, [5]),
            FreshsalesClient.plan_remove_contacts(7, remove_all=True),
            FreshsalesClient.plan_move_contacts(8, 7, [6]),
            FreshsalesClient.plan_move_contacts(8, 7, move_all=True),
        ]
        self.assertEqual(
            [(p.method, p.path, dict(p.body)) for p in plans],
            [
                ("POST", "/lists", {"name": "Analysts"}),
                ("PUT", "/lists/7", {"name": "Analysts 2026"}),
                ("PUT", "/lists/7/add_contacts", {"ids": [3, 4]}),
                ("PUT", "/lists/7/remove_contacts", {"ids": [5]}),
                ("PUT", "/lists/7/remove_contacts", {"all": True}),
                ("PUT", "/lists/8/move_contacts", {"ids": [6], "from_list_id": 7}),
                ("PUT", "/lists/8/move_contacts", {"from_list_id": 7}),
            ],
        )

    def test_invalid_plans_are_rejected(self) -> None:
        cases = [
            lambda: FreshsalesClient.plan_create_list(" "),
            lambda: FreshsalesClient.plan_create_list("x" * 256),
            lambda: FreshsalesClient.plan_rename_list("abc", "n"),
            lambda: FreshsalesClient.plan_add_contacts(1, []),
            lambda: FreshsalesClient.plan_add_contacts(1, ["-3"]),
            lambda: FreshsalesClient.plan_add_contacts(1, ["1; drop"]),
            lambda: FreshsalesClient.plan_remove_contacts(1, [2], remove_all=True),
            lambda: FreshsalesClient.plan_move_contacts(1, 1, [2]),
            lambda: FreshsalesClient.plan_move_contacts(2, 1, [3], move_all=True),
        ]
        for case in cases:
            with self.subTest(case=case), self.assertRaises(ValueError):
                case()

    def test_batching_and_per_command_cap(self) -> None:
        plans = writes.plan_add("1", [str(n) for n in range(1, 251)], batch_size=100)
        self.assertEqual([len(p.body["ids"]) for p in plans], [100, 100, 50])
        with self.assertRaisesRegex(ValueError, "cap"):
            writes.plan_add("1", [str(n) for n in range(1, 1002)], batch_size=100)
        with self.assertRaisesRegex(ValueError, "batch size"):
            writes.plan_add("1", ["1"], batch_size=501)

    def test_all_operations_require_matching_confirmation(self) -> None:
        with self.assertRaisesRegex(ValueError, "--confirm 1"):
            writes.plan_remove("1", [], remove_all=True, confirm=None, batch_size=100)
        with self.assertRaisesRegex(ValueError, "--confirm 1"):
            writes.plan_remove("1", [], remove_all=True, confirm="2", batch_size=100)
        # For moves the list being emptied is the source list.
        with self.assertRaisesRegex(ValueError, "--confirm 7"):
            writes.plan_move("8", "7", [], move_all=True, confirm="8", batch_size=100)
        plans = writes.plan_move(
            "8", "7", [], move_all=True, confirm="7", batch_size=100
        )
        self.assertEqual(dict(plans[0].body), {"from_list_id": 7})

    def test_all_operations_reject_explicit_ids(self) -> None:
        with self.assertRaisesRegex(ValueError, "not both"):
            writes.plan_remove("1", ["3"], remove_all=True, confirm="1", batch_size=100)
        with self.assertRaisesRegex(ValueError, "not both"):
            writes.plan_move(
                "8", "7", ["3"], move_all=True, confirm="7", batch_size=100
            )


class IdInputTests(unittest.TestCase):
    def test_ids_argument_plain_file_csv_and_stdin(self) -> None:
        files = {
            "plain.txt": "# analysts\n10\n\n11\n",
            "report.csv": 'id,display_name,email\n12,"Lyst, Ana",a@x.example\n13,B,b\n',
        }
        opener = lambda path: io.StringIO(files[path])
        self.assertEqual(
            writes.read_contact_ids("1, 2", "plain.txt", opener=opener),
            ["1", "2", "10", "11"],
        )
        self.assertEqual(
            writes.read_contact_ids(None, "report.csv", opener=opener), ["12", "13"]
        )
        self.assertEqual(
            writes.read_contact_ids(None, "-", stdin=io.StringIO("14\n15\n")),
            ["14", "15"],
        )

    def test_csv_without_id_column_is_rejected(self) -> None:
        opener = lambda path: io.StringIO("email\na@x.example\n")
        with self.assertRaisesRegex(ValueError, "'id' column"):
            writes.read_contact_ids(None, "f.csv", opener=opener)

    def test_ids_file_is_read_from_disk_and_empty_input_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory, "ids.txt")
            path.write_text("21\n22\n", encoding="utf-8")
            self.assertEqual(writes.read_contact_ids(None, str(path)), ["21", "22"])
            path.write_text("# nothing yet\n\n", encoding="utf-8")
            self.assertEqual(writes.read_contact_ids(None, str(path)), [])
            # An empty ID set never becomes a request.
            with self.assertRaisesRegex(ValueError, "at least one contact id"):
                writes.plan_add("1", [], batch_size=100)

    def test_unreadable_ids_file_is_a_clean_error(self) -> None:
        def opener(path: str) -> io.StringIO:
            raise FileNotFoundError(2, "No such file or directory", path)

        with self.assertRaisesRegex(ValueError, "could not read ids file"):
            writes.read_contact_ids(None, "missing.txt", opener=opener)


class RunTests(unittest.TestCase):
    def test_dry_run_sends_nothing(self) -> None:
        client = FakeWriteClient()
        plans = writes.plan_add("1", ["3"], batch_size=100)
        report = writes.run(client, plans, base_url=BASE, apply=False)
        self.assertTrue(report["dry_run"])
        self.assertEqual(report["requests"][0]["url"], f"{BASE}/lists/1/add_contacts")
        self.assertEqual(client.sent, [])

    def test_apply_requires_a_client(self) -> None:
        with self.assertRaisesRegex(ValueError, "client is required"):
            writes.run(
                None,
                writes.plan_add("1", ["3"], batch_size=100),
                base_url=BASE,
                apply=True,
            )

    def test_apply_without_preflight_reports_no_targets(self) -> None:
        client = FakeWriteClient()
        report = writes.run(
            client,
            writes.plan_add("1", ["3"], batch_size=100),
            base_url=BASE,
            apply=True,
        )
        self.assertNotIn("targets", report)
        self.assertEqual(
            report["results"], [{"request": 0, "response": {"message": "ok"}}]
        )

    def test_apply_resolves_targets_then_sends(self) -> None:
        client = FakeWriteClient()
        plans = writes.plan_move(
            "2", "1", ["3"], move_all=False, confirm=None, batch_size=100
        )
        report = writes.run(
            client,
            plans,
            base_url=BASE,
            apply=True,
            preflight=lambda c: writes.resolve_lists(
                c, {"list": "2", "from_list": "1"}
            ),
        )
        self.assertFalse(report["dry_run"])
        self.assertEqual(report["targets"]["from_list"]["name"], "Product Launch")
        self.assertEqual(len(client.sent), 1)

    def test_missing_target_list_blocks_every_write(self) -> None:
        client = FakeWriteClient()
        with self.assertRaisesRegex(ValueError, "list 99 was not found"):
            writes.run(
                client,
                writes.plan_add("99", ["3"], batch_size=100),
                base_url=BASE,
                apply=True,
                preflight=lambda c: writes.resolve_lists(c, {"list": "99"}),
            )
        self.assertEqual(client.sent, [])

    def test_duplicate_list_name_is_refused(self) -> None:
        with self.assertRaisesRegex(ValueError, "already exists"):
            writes.ensure_name_is_free(FakeWriteClient(), " analysts ")

    def test_list_lookup_walks_pages_and_refuses_past_the_cap(self) -> None:
        class PagedListClient(FakeWriteClient):
            def __init__(self, total_pages: int) -> None:
                super().__init__()
                self.total_pages = total_pages
                self.pages: list[int] = []

            def marketing_lists(self, *, page: int = 1) -> Any:
                self.pages.append(page)
                return {
                    "lists": [{"id": page, "name": f"List {page}"}],
                    "meta": {"total_pages": self.total_pages},
                }

        client = PagedListClient(3)
        resolved = writes.resolve_lists(client, {"list": "3"})
        self.assertEqual(resolved, {"list": {"id": 3, "name": "List 3"}})
        self.assertEqual(client.pages, [1, 2, 3])

        client = PagedListClient(writes.MAX_LIST_LOOKUP_PAGES + 1)
        with self.assertRaisesRegex(FreshsalesError, "refusing to guess"):
            writes.resolve_lists(client, {"list": "1"})
        self.assertEqual(len(client.pages), writes.MAX_LIST_LOOKUP_PAGES)


class CliWriteTests(unittest.TestCase):
    def test_dry_run_needs_no_api_key_or_client(self) -> None:
        code, stdout, stderr = run_cli(
            ["lists", "add-contacts", "1", "--ids", "3,4"],
            api_key_loader=never_load_key,
        )
        self.assertEqual((code, stderr), (0, ""))
        report = json.loads(stdout)
        self.assertTrue(report["dry_run"])
        self.assertEqual(
            report["requests"][0]["url"],
            "https://example.myfreshworks.com/crm/sales/api/lists/1/add_contacts",
        )
        self.assertEqual(report["requests"][0]["body"], {"ids": [3, 4]})

    def test_apply_loads_the_key(self) -> None:
        def missing_key() -> str:
            raise CredentialError("no key")

        code, stdout, stderr = run_cli(
            ["lists", "create", "Analysts", "--apply"], api_key_loader=missing_key
        )
        self.assertEqual((code, stdout), (1, ""))
        self.assertIn("no key", stderr)

    def test_apply_with_injected_client_for_each_command(self) -> None:
        cases = [
            (["lists", "create", "New list"], "POST", "/lists"),
            (["lists", "rename", "1", "Renamed"], "PUT", "/lists/1"),
            (
                ["lists", "add-contacts", "1", "--ids", "3"],
                "PUT",
                "/lists/1/add_contacts",
            ),
            (
                ["lists", "create", "Analysts", "--allow-duplicate-name"],
                "POST",
                "/lists",
            ),
            (
                ["lists", "remove-contacts", "1", "--ids", "3,4"],
                "PUT",
                "/lists/1/remove_contacts",
            ),
            (
                ["lists", "remove-contacts", "1", "--all", "--confirm", "1"],
                "PUT",
                "/lists/1/remove_contacts",
            ),
            (
                ["lists", "move-contacts", "2", "--from-list", "1", "--all"]
                + ["--confirm", "1"],
                "PUT",
                "/lists/2/move_contacts",
            ),
            (
                ["lists", "move-contacts", "2", "--from-list", "1", "--ids", "3"],
                "PUT",
                "/lists/2/move_contacts",
            ),
        ]
        for argv, method, path in cases:
            with self.subTest(argv=argv):
                client = FakeWriteClient()
                code, _, stderr = run_cli(argv + ["--apply"], client=client)
                self.assertEqual((code, stderr), (0, ""))
                self.assertEqual(
                    [(r.method, r.path) for r in client.sent], [(method, path)]
                )

    def test_remove_all_without_confirm_fails_cleanly(self) -> None:
        client = FakeWriteClient()
        code, stdout, stderr = run_cli(
            ["lists", "remove-contacts", "1", "--all", "--apply"], client=client
        )
        self.assertEqual((code, stdout), (1, ""))
        self.assertIn("--confirm 1", stderr)
        self.assertEqual(client.sent, [])

    def test_partial_failure_reports_what_was_applied(self) -> None:
        client = FakeWriteClient(fail_on=1)
        code, stdout, stderr = run_cli(
            [
                "lists", "add-contacts", "1", "--ids", "3,4,5",
                "--batch-size", "2", "--apply",
            ],
            client=client,
        )  # fmt: skip
        self.assertEqual(code, 1)
        report = json.loads(stdout)
        self.assertEqual(len(report["results"]), 1)
        self.assertEqual(report["failed_request"], 1)
        self.assertIn("1 already applied", stderr)
        self.assertIn("429", stderr)


class ClientGateTests(unittest.TestCase):
    def test_send_is_blocked_unless_writes_are_enabled(self) -> None:
        transport = RecordingTransport()
        with self.assertRaises(WriteDisabledError):
            make_client(transport).add_contacts_to_list(1, [2])
        self.assertEqual(transport.requests, [])

    def test_enabled_client_sends_the_planned_body(self) -> None:
        transport = RecordingTransport()
        client = make_client(transport, allow_writes=True)
        client.create_list("Analysts")
        client.rename_list(1, "Analysts 2026")
        client.add_contacts_to_list(1, [2, 3])
        client.remove_contacts_from_list(1, remove_all=True)
        client.move_contacts_between_lists(2, 1, [3])
        self.assertEqual(
            [
                (r["method"], urlsplit(r["url"]).path, json.loads(r["body"]))
                for r in transport.requests
            ],
            [
                ("POST", "/crm/sales/api/lists", {"name": "Analysts"}),
                ("PUT", "/crm/sales/api/lists/1", {"name": "Analysts 2026"}),
                ("PUT", "/crm/sales/api/lists/1/add_contacts", {"ids": [2, 3]}),
                ("PUT", "/crm/sales/api/lists/1/remove_contacts", {"all": True}),
                (
                    "PUT",
                    "/crm/sales/api/lists/2/move_contacts",
                    {"ids": [3], "from_list_id": 1},
                ),
            ],
        )


if __name__ == "__main__":
    unittest.main()


REVIEW_CSV = (
    "﻿contact_id,first_name,current_linkedin,suggested_linkedin,confidence\n"
    "11,Ana,https://www.linkedin.com/in/ana-x,ana-x,existing\n"
    "12,Bob,,bob-y-1a2b3c,high\n"
    "13,Cy,,,none\n"
)


class FakeContactClient:
    """Contacts in memory keyed by ID; records every write that is sent."""

    base_url = BASE

    def __init__(self, contacts: dict[str, dict[str, Any]] | None = None) -> None:
        self.contacts = (
            contacts
            if contacts is not None
            else {
                "11": {"id": 11, "linkedin": "https://www.linkedin.com/in/ana-x"},
                "12": {"id": 12, "linkedin": None},
            }
        )
        self.fetched: list[str] = []
        self.sent: list[WriteRequest] = []

    def get_contact(self, contact_id: str, **_: Any) -> Any:
        self.fetched.append(contact_id)
        if contact_id not in self.contacts:
            raise ApiError(404, "not found")
        return {"contact": self.contacts[contact_id]}

    def send(self, request: WriteRequest) -> Any:
        self.sent.append(request)
        return {"contact": {"id": request.path.rsplit("/", 1)[-1]}}


def review_rows(text: str = REVIEW_CSV) -> list[dict[str, str]]:
    return writes.read_linkedin_updates(
        "review.csv", opener=lambda _: io.StringIO(text)
    )


class LinkedinPlanTests(unittest.TestCase):
    def test_payload_changes_only_the_linkedin_field(self) -> None:
        plan = FreshsalesClient.plan_set_contact_linkedin(" 12 ", " bob-y-1a2b3c ")
        self.assertEqual(
            (plan.method, plan.path, dict(plan.body)),
            ("PUT", "/contacts/12", {"contact": {"linkedin": "bob-y-1a2b3c"}}),
        )

    def test_alias_and_contact_id_are_validated(self) -> None:
        for alias in [
            "gwena%C3%ABl-durand-0b0457b1",
            "under_score",
            "hartvik",
            "x" * 100,
        ]:
            with self.subTest(alias=alias):
                FreshsalesClient.plan_set_contact_linkedin(1, alias)
        for alias in [
            "",
            "a",
            "https://www.linkedin.com/in/ana-x",
            "linkedin.com/in/ana-x",
            "ana/x",
            "ana.x",
            "-leading-hyphen",
            "bad%zz",
            "x" * 101,
        ]:
            with self.subTest(alias=alias), self.assertRaisesRegex(ValueError, "alias"):
                FreshsalesClient.plan_set_contact_linkedin(1, alias)
        with self.assertRaisesRegex(ValueError, "contact id"):
            FreshsalesClient.plan_set_contact_linkedin("abc", "hartvik")


class LinkedinCsvTests(unittest.TestCase):
    def test_review_table_is_read_and_empty_suggestions_are_skipped(self) -> None:
        self.assertEqual(
            review_rows(),
            [
                {
                    "contact_id": "11",
                    "current_linkedin": "https://www.linkedin.com/in/ana-x",
                    "suggested_linkedin": "ana-x",
                },
                {
                    "contact_id": "12",
                    "current_linkedin": "",
                    "suggested_linkedin": "bob-y-1a2b3c",
                },
            ],
        )

    def test_disk_and_stdin_sources(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory, "review.csv")
            path.write_text(REVIEW_CSV, encoding="utf-8")
            self.assertEqual(len(writes.read_linkedin_updates(str(path))), 2)
        rows = writes.read_linkedin_updates("-", stdin=io.StringIO(REVIEW_CSV))
        self.assertEqual([row["contact_id"] for row in rows], ["11", "12"])

    def test_invalid_csv_input_is_rejected(self) -> None:
        header = "contact_id,current_linkedin,suggested_linkedin\n"
        cases = [
            ("contact_id,suggested_linkedin\n1,a-b\n", "must have columns"),
            (header + "1,,a-b\n1,,c-d\n", "more than once"),
            (header + "1,,\n", "no rows"),
            (
                header
                + "".join(
                    f"{n},,alias-{n}\n"
                    for n in range(1, writes.MAX_CONTACT_UPDATES + 2)
                ),
                "cap",
            ),
        ]
        for text, message in cases:
            with (
                self.subTest(message=message),
                self.assertRaisesRegex(ValueError, message),
            ):
                review_rows(text)

    def test_unreadable_csv_is_a_clean_error(self) -> None:
        def opener(path: str) -> io.StringIO:
            raise FileNotFoundError(2, "No such file or directory", path)

        with self.assertRaisesRegex(ValueError, "could not read csv file"):
            writes.read_linkedin_updates("missing.csv", opener=opener)


class LinkedinRunTests(unittest.TestCase):
    def test_unchanged_contacts_are_checked_before_any_write(self) -> None:
        client = FakeContactClient()
        rows = review_rows()
        report = writes.run(
            client,
            writes.plan_set_linkedin(rows),
            base_url=BASE,
            apply=True,
            preflight=lambda c: writes.verify_linkedin_unchanged(c, rows),
        )
        self.assertEqual(client.fetched, ["11", "12"])
        self.assertEqual(report["targets"], {"contacts_checked": 2})
        self.assertEqual(
            [(r.path, dict(r.body)) for r in client.sent],
            [
                ("/contacts/11", {"contact": {"linkedin": "ana-x"}}),
                ("/contacts/12", {"contact": {"linkedin": "bob-y-1a2b3c"}}),
            ],
        )

    def test_a_changed_or_missing_contact_blocks_the_whole_run(self) -> None:
        rows = review_rows()
        ana = {"linkedin": "https://www.linkedin.com/in/ana-x"}
        for contacts, error, message in [
            (
                {"11": ana, "12": {"linkedin": "someone-else"}},
                ValueError,
                "changed since the review for contact\\(s\\) 12",
            ),
            ({"11": ana}, ApiError, "404"),
        ]:
            client = FakeContactClient(contacts)
            with self.subTest(message=message), self.assertRaisesRegex(error, message):
                writes.run(
                    client,
                    writes.plan_set_linkedin(rows),
                    base_url=BASE,
                    apply=True,
                    preflight=lambda c: writes.verify_linkedin_unchanged(c, rows),
                )
            self.assertEqual(client.sent, [])

    def test_contact_payload_without_a_contact_is_not_found(self) -> None:
        class EmptyClient(FakeContactClient):
            def get_contact(self, contact_id: str, **_: Any) -> Any:
                return {}

        with self.assertRaisesRegex(FreshsalesError, "contact 11 was not found"):
            writes.verify_linkedin_unchanged(EmptyClient(), review_rows()[:1])


class LinkedinCliTests(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.csv = str(Path(directory.name, "review.csv"))
        Path(self.csv).write_text(REVIEW_CSV, encoding="utf-8")

    def test_dry_run_needs_no_api_key_or_client(self) -> None:
        code, stdout, stderr = run_cli(
            ["contacts", "set-linkedin", "--csv", self.csv],
            api_key_loader=never_load_key,
        )
        self.assertEqual((code, stderr), (0, ""))
        report = json.loads(stdout)
        self.assertTrue(report["dry_run"])
        self.assertEqual(
            [(r["method"], r["url"], r["body"]) for r in report["requests"]],
            [
                (
                    "PUT",
                    "https://example.myfreshworks.com/crm/sales/api/contacts/11",
                    {"contact": {"linkedin": "ana-x"}},
                ),
                (
                    "PUT",
                    "https://example.myfreshworks.com/crm/sales/api/contacts/12",
                    {"contact": {"linkedin": "bob-y-1a2b3c"}},
                ),
            ],
        )

    def test_apply_checks_then_sends(self) -> None:
        client = FakeContactClient()
        code, stdout, stderr = run_cli(
            ["contacts", "set-linkedin", "--csv", self.csv, "--apply"], client=client
        )
        self.assertEqual((code, stderr), (0, ""))
        self.assertFalse(json.loads(stdout)["dry_run"])
        self.assertEqual(
            [r.path for r in client.sent], ["/contacts/11", "/contacts/12"]
        )

    def test_stale_review_fails_cleanly_and_sends_nothing(self) -> None:
        client = FakeContactClient({"11": {"linkedin": "edited"}, "12": {}})
        code, stdout, stderr = run_cli(
            ["contacts", "set-linkedin", "--csv", self.csv, "--apply"], client=client
        )
        self.assertEqual((code, stdout), (1, ""))
        self.assertIn("changed since the review", stderr)
        self.assertEqual(client.sent, [])
