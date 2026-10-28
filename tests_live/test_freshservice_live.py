"""Live Freshservice ticket write test (opt-in).

Run: ``FRESHWORKS_LIVE_WRITE=1 FRESHSERVICE_BASE_URL=<url>
FRESHSERVICE_LIVE_WORKSPACE_ID=<id>
python -m unittest discover -s tests_live -t . -p 'test_freshservice_live.py' -v``

Creates one [CLI-TEST] ticket requested by the agent who owns the API key,
updates it, adds a private note, checks each step with GET, then deletes the
ticket and verifies it is gone. Output is limited to counts and outcomes.
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import unittest
from typing import Any

from freshservice_cli.auth import load_api_key
from freshservice_cli.cli import main
from freshservice_cli.client import BASE_URL_ENV, FreshserviceClient

from .adapters import freshservice_adapters
from .harness import (
    LIVE_FLAG,
    NAME_PREFIX,
    Tracker,
    base_url,
    live_enabled,
    new_run_tag,
)

WORKSPACE_ENV = "FRESHSERVICE_LIVE_WORKSPACE_ID"


def cli(*argv: str) -> dict[str, Any]:
    stdout, stderr = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
        code = main(list(argv))
    if code != 0:
        raise AssertionError(
            f"freshservice {argv[0]} {argv[1]} failed: {stderr.getvalue()}"
        )
    return json.loads(stdout.getvalue())


@unittest.skipUnless(live_enabled(), f"set {LIVE_FLAG}=1 to run live write tests")
class FreshserviceTicketLiveTest(unittest.TestCase):
    def setUp(self) -> None:
        workspace = os.environ.get(WORKSPACE_ENV)
        if not workspace:
            self.fail(f"set {WORKSPACE_ENV} to the workspace for test tickets")
        self.workspace = workspace
        url = base_url(BASE_URL_ENV)
        self.reader = FreshserviceClient(load_api_key(), base_url=url)
        writer = FreshserviceClient(load_api_key(), base_url=url, allow_writes=True)
        self.run_tag = new_run_tag()
        self.tracker = Tracker(self.run_tag, freshservice_adapters(writer))
        # Cleanup runs even when an assertion fails part-way.
        self.addCleanup(self._cleanup)

    def _cleanup(self) -> None:
        results = self.tracker.cleanup()
        print(f"\ncleanup ({self.run_tag}): {results}")

    def test_ticket_create_update_note_and_verified_delete(self) -> None:
        me = self.reader.get_current_agent()["agent"]["id"]
        created = cli(
            "tickets",
            "create",
            "--subject",
            f"{NAME_PREFIX} live write test {self.run_tag}",
            "--description",
            "Automated freshworks-cli live test. Safe to ignore.",
            "--workspace-id",
            self.workspace,
            "--requester-id",
            str(me),
            "--tag",
            self.run_tag,
            "--apply",
        )
        ticket_id = created["results"][0]["response"]["ticket"]["id"]
        self.tracker.register("freshservice", "tickets", ticket_id)

        ticket = self.reader.get_ticket(ticket_id, include="tags")["ticket"]
        self.assertIn(self.run_tag, ticket["tags"])
        self.assertEqual((ticket["status"], ticket["priority"]), (2, 1))

        updated = cli(
            "tickets",
            "update",
            str(ticket_id),
            "--status",
            "3",
            "--priority",
            "2",
            "--tag",
            self.run_tag,
            "--apply",
        )
        self.assertEqual(updated["target"]["status"], 2)
        ticket = self.reader.get_ticket(ticket_id, include="tags")["ticket"]
        self.assertEqual((ticket["status"], ticket["priority"]), (3, 2))

        cli(
            "tickets",
            "add-note",
            str(ticket_id),
            "--text",
            f"{NAME_PREFIX} private note",
            "--apply",
        )
        conversations = self.reader.ticket_conversations(ticket_id)["conversations"]
        notes = [
            c
            for c in conversations
            if NAME_PREFIX in (c.get("body_text") or c.get("body") or "")
        ]
        self.assertEqual(len(notes), 1)
        self.assertTrue(notes[0]["private"])

        self.assertEqual(
            self.tracker.cleanup(), {f"freshservice:tickets:{ticket_id}": "deleted"}
        )


if __name__ == "__main__":
    unittest.main()
