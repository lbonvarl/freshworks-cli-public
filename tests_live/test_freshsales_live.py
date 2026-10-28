"""Live Freshsales record write test (opt-in).

Run: ``FRESHWORKS_LIVE_WRITE=1 FRESHSALES_BASE_URL=<url>
FRESHSALES_LIVE_OWNER_ID=<user id> [FRESHSALES_LIVE_ACCOUNT_FIELDS='f=v;...']
python -m unittest discover -s tests_live -t . -p 'test_freshsales_live.py' -v``

Creates one [CLI-TEST] account, contact, deal, task and note owned by the
given user, updates the account, contact, deal and task, checks each step
with GET, then deletes everything (children first) and verifies each record
is gone. The contact uses an unsubscribed @example.com address so nothing is
emailed. Fields your account form requires go in FRESHSALES_LIVE_ACCOUNT_FIELDS.
Output is limited to counts and outcomes.
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import unittest
from datetime import UTC, date, datetime, timedelta
from typing import Any

from freshsales_cli.auth import load_api_key
from freshsales_cli.cli import main
from freshsales_cli.client import BASE_URL_ENV, FreshsalesClient

from .adapters import freshsales_adapters
from .harness import (
    LIVE_FLAG,
    NAME_PREFIX,
    Tracker,
    base_url,
    live_enabled,
    new_run_tag,
)

OWNER_ENV = "FRESHSALES_LIVE_OWNER_ID"
UNSUBSCRIBED = "0"
ACCOUNT_FIELDS_ENV = "FRESHSALES_LIVE_ACCOUNT_FIELDS"


def account_fields() -> list[str]:
    """Extra ``FIELD=VALUE`` pairs for the test account, separated by ``;``.

    Use it for the fields your account form requires, and turn off any
    screening or compliance checkbox your account has, e.g.
    ``cf_region=EMEA;cf_screening_check=false``.
    """

    raw = os.environ.get(ACCOUNT_FIELDS_ENV, "")
    return [item.strip() for item in raw.split(";") if item.strip()]


def cli(*argv: str) -> dict[str, Any]:
    stdout, stderr = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
        code = main(list(argv))
    if code != 0:
        raise AssertionError(
            f"freshsales {argv[0]} {argv[1]} failed: {stderr.getvalue()}"
        )
    return json.loads(stdout.getvalue())


def sets(*pairs: str) -> list[str]:
    return [item for pair in pairs for item in ("--set", pair)]


def created_id(report: dict[str, Any], key: str) -> int:
    return int(report["results"][0]["response"][key]["id"])


@unittest.skipUnless(live_enabled(), f"set {LIVE_FLAG}=1 to run live write tests")
class FreshsalesRecordLiveTest(unittest.TestCase):
    def setUp(self) -> None:
        owner = os.environ.get(OWNER_ENV)
        if not owner:
            self.fail(f"set {OWNER_ENV} to the Freshsales user who owns test records")
        self.owner = owner
        url = base_url(BASE_URL_ENV)
        self.reader = FreshsalesClient(load_api_key(), base_url=url)
        writer = FreshsalesClient(load_api_key(), base_url=url, allow_writes=True)
        self.run_tag = new_run_tag()
        self.tracker = Tracker(self.run_tag, freshsales_adapters(writer))
        self.addCleanup(self._cleanup)

    def _cleanup(self) -> None:
        results = self.tracker.cleanup()
        print(f"\ncleanup ({self.run_tag}): {results}")

    def test_record_writes_and_verified_delete(self) -> None:
        label = f"{NAME_PREFIX} {self.run_tag}"
        tag = ["--tag", self.run_tag, "--apply"]

        # Account --------------------------------------------------------
        report = cli(
            "accounts",
            "create",
            *sets(f"name={label} account", f"owner_id={self.owner}", *account_fields()),
            *tag,
        )
        account_id = created_id(report, "sales_account")
        self.tracker.register("freshsales", "accounts", account_id)
        cli("accounts", "update", str(account_id), *sets("city=Updated City"), *tag)
        account = self.reader.get_account(account_id)["sales_account"]
        self.assertEqual(account["city"], "Updated City")
        self.assertIn(self.run_tag, account.get("tags") or [])

        # Contact --------------------------------------------------------
        report = cli(
            "contacts",
            "create",
            *sets(
                "first_name=CLI",
                f"last_name={label} contact",
                f"email={self.run_tag}@example.com",
                f"subscription_status={UNSUBSCRIBED}",
                f"owner_id={self.owner}",
            ),
            *tag,
        )
        contact_id = created_id(report, "contact")
        self.tracker.register("freshsales", "contacts", contact_id)
        cli(
            "contacts", "update", str(contact_id), *sets("job_title=Test Analyst"), *tag
        )
        contact = self.reader.get_contact(contact_id)["contact"]
        self.assertEqual(contact["job_title"], "Test Analyst")

        # Deal -----------------------------------------------------------
        pipeline = self.reader.selector("deal_pipelines")["deal_pipelines"][0]["id"]
        close = (date.today() + timedelta(days=30)).isoformat()
        report = cli(
            "deals",
            "create",
            *sets(
                f"name={label} deal",
                "amount=1",
                f"sales_account_id={account_id}",
                f"deal_pipeline_id={pipeline}",
                f"expected_close={close}",
                f"owner_id={self.owner}",
            ),
            *tag,
        )
        deal_id = created_id(report, "deal")
        self.tracker.register("freshsales", "deals", deal_id)
        cli("deals", "update", str(deal_id), *sets("amount=2"), *tag)
        deal = self.reader.get_deal(deal_id)["deal"]
        self.assertEqual(float(deal["amount"]), 2.0)

        # Task on the contact -------------------------------------------
        due = (datetime.now(UTC) + timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
        report = cli(
            "tasks",
            "create",
            *sets(
                f"title={label} task",
                f"due_date={due}",
                f"owner_id={self.owner}",
                "targetable_type=Contact",
                f"targetable_id={contact_id}",
            ),
            "--apply",
        )
        task_id = created_id(report, "task")
        self.tracker.register("freshsales", "tasks", task_id)
        cli("tasks", "update", str(task_id), *sets("status=1"), "--apply")
        task = self.reader.get_task(task_id)["task"]
        self.assertEqual(int(task["status"]), 1)

        # Note on the deal ----------------------------------------------
        report = cli(
            "notes",
            "create",
            *sets(
                f"description={label} note",
                "targetable_type=Deal",
                f"targetable_id={deal_id}",
            ),
            "--apply",
        )
        note_id = created_id(report, "note")
        self.tracker.register("freshsales", "notes", note_id, parent=f"deals:{deal_id}")
        notes = self.reader.get_deal(deal_id, include="notes").get("notes") or []
        self.assertIn(note_id, [note["id"] for note in notes])

        # Delete children first and verify each is gone ------------------
        results = self.tracker.cleanup()
        self.assertEqual(set(results.values()), {"deleted"}, results)
        self.assertEqual(len(results), 5)


if __name__ == "__main__":
    unittest.main()
