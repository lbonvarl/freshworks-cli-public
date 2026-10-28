from __future__ import annotations

import unittest
from datetime import UTC, timedelta
from typing import Any

from freshsales_cli import analytics


class FakeAudienceClient:
    """In-memory audience: pages of contacts plus per-contact timelines."""

    def __init__(
        self,
        pages: list[list[dict[str, Any]]],
        timelines: dict[int, list[dict[str, Any]]],
    ) -> None:
        self.pages = pages
        self.timelines = timelines
        self.page_calls: list[tuple[str, object, int]] = []
        self.timeline_calls: list[object] = []

    def _page(self, kind: str, source: object, page: int) -> dict[str, Any]:
        self.page_calls.append((kind, source, page))
        contacts = self.pages[page - 1] if page <= len(self.pages) else []
        return {"contacts": contacts, "meta": {"total_pages": len(self.pages)}}

    def marketing_list_contacts(self, list_id: object, *, page: int = 1) -> Any:
        return self._page("list", list_id, page)

    def list_contacts(self, view_id: object, *, page: int = 1) -> Any:
        return self._page("view", view_id, page)

    def contact_activities(
        self, contact_id: object, *, include: object = None, limit: object = None
    ) -> Any:
        self.timeline_calls.append(contact_id)
        return {"activities": self.timelines.get(contact_id, [])}  # type: ignore[arg-type]


def opened(when: str, campaign: str = "Product Launch") -> dict[str, Any]:
    return {
        "action_type": "EMAIL_OPENED",
        "created_at": when,
        "action_data": {"campaign_name": campaign, "subject": "Hello"},
    }


CONTACTS = [
    {"id": 1, "display_name": "Ana", "job_title": "Principal Analyst",
     "email": "ana@firm.example", "sales_accounts": [{"name": "Firm"}],
     "custom_field": {"cf_persona": "Industry analyst"}},
    {"id": 2, "display_name": "Bob", "job_title": "Research Analyst"},
    {"id": 3, "display_name": "Cy", "job_title": "CTO"},
]  # fmt: skip


class EngagementTests(unittest.TestCase):
    def test_filters_contacts_activities_campaign_and_since(self) -> None:
        client = FakeAudienceClient(
            [CONTACTS],
            {
                1: [
                    opened("2026-09-10T09:00:00+02:00"),
                    opened("2026-06-01T09:00:00+00:00"),  # before --since
                    {
                        "action_type": "EMAIL_CLICKED",
                        "created_at": "2026-09-11T00:00:00Z",
                    },
                ],
                2: [opened("2026-09-12T09:00:00Z", campaign="Smart meters")],
                3: [opened("2026-09-12T09:00:00Z")],
            },
        )
        report = analytics.campaign_engagement(
            client,
            analytics.Audience(list_id="9"),
            analytics.EngagementFilters(
                campaign="launch",
                since="2026-09-01",
                contact_filters=(analytics.FieldMatch.parse("job_title=analyst"),),
            ),
        )

        self.assertEqual(client.timeline_calls, [1, 2])  # CTO never fetched
        self.assertEqual(report["scanned_contacts"], 3)
        self.assertEqual(report["eligible_contacts"], 2)
        self.assertEqual(report["engaged_contacts"], 1)
        self.assertFalse(report["truncated"])
        self.assertEqual(report["action_type_counts"], {"EMAIL_OPENED": 1})
        row = report["contacts"][0]
        self.assertEqual(
            (row["id"], row["company"], row["event_count"]), (1, "Firm", 1)
        )

    def test_dotted_field_match_and_view_audience(self) -> None:
        client = FakeAudienceClient([CONTACTS], {1: [opened("2026-09-10T00:00:00Z")]})
        report = analytics.campaign_engagement(
            client,
            analytics.Audience(view_id="4"),
            analytics.EngagementFilters(
                contact_filters=(
                    analytics.FieldMatch.parse("custom_field.cf_persona=analyst"),
                )
            ),
        )
        self.assertEqual(client.page_calls, [("view", "4", 1)])
        self.assertEqual([row["id"] for row in report["contacts"]], [1])

    def test_limits_bound_pages_and_contacts(self) -> None:
        pages = [[{"id": n}] for n in range(1, 6)]
        client = FakeAudienceClient(pages, {})
        report = analytics.campaign_engagement(
            client,
            analytics.Audience(list_id="9"),
            analytics.EngagementFilters(),
            analytics.Limits(max_pages=2),
        )
        self.assertEqual(len(client.page_calls), 2)
        self.assertEqual(report["scanned_contacts"], 2)
        self.assertTrue(report["truncated"])

        client = FakeAudienceClient([CONTACTS], {})
        report = analytics.campaign_engagement(
            client,
            analytics.Audience(list_id="9"),
            analytics.EngagementFilters(),
            analytics.Limits(max_contacts=2),
        )
        self.assertEqual(client.timeline_calls, [1, 2])
        self.assertEqual(report["scanned_contacts"], 2)
        self.assertTrue(report["truncated"])

        # Filtered-out contacts do not consume the timeline budget.
        client = FakeAudienceClient([CONTACTS], {})
        analytics.campaign_engagement(
            client,
            analytics.Audience(list_id="9"),
            analytics.EngagementFilters(
                contact_filters=(analytics.FieldMatch.parse("job_title=CTO"),)
            ),
            analytics.Limits(max_contacts=1),
        )
        self.assertEqual(client.timeline_calls, [3])

    def test_delay_is_applied_between_timeline_calls(self) -> None:
        sleeps: list[float] = []
        client = FakeAudienceClient([CONTACTS], {})
        analytics.campaign_engagement(
            client,
            analytics.Audience(list_id="9"),
            analytics.EngagementFilters(),
            analytics.Limits(delay=0.5),
            sleep=sleeps.append,
        )
        self.assertEqual(sleeps, [0.5, 0.5])

    def test_csv_has_one_row_per_engaged_contact(self) -> None:
        client = FakeAudienceClient([CONTACTS], {2: [opened("2026-09-10T00:00:00Z")]})
        report = analytics.campaign_engagement(
            client, analytics.Audience(list_id="9"), analytics.EngagementFilters()
        )
        lines = analytics.engagement_csv(report).splitlines()
        self.assertEqual(lines[0], ",".join(analytics.CSV_COLUMNS))
        self.assertEqual(len(lines), 2)
        self.assertTrue(lines[1].startswith("2,Bob,"))

    def test_invalid_inputs_are_rejected(self) -> None:
        with self.assertRaises(ValueError):
            analytics.Audience()
        with self.assertRaises(ValueError):
            analytics.Audience(list_id="1", view_id="2")
        with self.assertRaisesRegex(ValueError, "FIELD=REGEX"):
            analytics.FieldMatch.parse("job_title")
        with self.assertRaisesRegex(ValueError, "regex"):
            analytics.EngagementFilters(action_pattern="(")
        with self.assertRaisesRegex(ValueError, "since"):
            analytics.EngagementFilters(since="last week")
        for field, value in [
            ("max_pages", 0),
            ("max_contacts", 0),
            ("activity_limit", 0),
            ("delay", -1.0),
        ]:
            with self.subTest(field=field), self.assertRaisesRegex(ValueError, field):
                analytics.Limits(**{field: value})


class EdgeCaseTests(unittest.TestCase):
    def test_malformed_audience_and_timeline_entries_are_skipped(self) -> None:
        class OddPagesClient(FakeAudienceClient):
            def _page(self, kind: str, source: object, page: int) -> dict[str, Any]:
                payload = super()._page(kind, source, page)
                payload["meta"] = {"total_pages": "n/a"}
                return payload

        client = OddPagesClient(
            [["not-a-contact", {"display_name": "No id"}, *CONTACTS[:1]]],
            {1: ["not-an-activity", opened("2026-09-10T00:00:00Z")]},
        )
        report = analytics.campaign_engagement(
            client, analytics.Audience(list_id="9"), analytics.EngagementFilters()
        )
        self.assertEqual(report["scanned_contacts"], 2)
        self.assertEqual(report["engaged_contacts"], 1)
        self.assertEqual(client.timeline_calls, [1])
        # total_pages is unreadable, so the walk asks for the next page.
        self.assertEqual([call[2] for call in client.page_calls], [1, 2])

    def test_page_without_meta_keeps_walking_until_an_empty_page(self) -> None:
        class NoMetaClient(FakeAudienceClient):
            def _page(self, kind: str, source: object, page: int) -> dict[str, Any]:
                payload = super()._page(kind, source, page)
                del payload["meta"]
                return payload

        client = NoMetaClient([CONTACTS[:1], CONTACTS[1:2]], {})
        report = analytics.campaign_engagement(
            client,
            analytics.Audience(view_id="4"),
            analytics.EngagementFilters(),
            analytics.Limits(max_pages=5),
        )
        self.assertEqual(report["scanned_contacts"], 2)
        self.assertFalse(report["truncated"])
        self.assertEqual([call[2] for call in client.page_calls], [1, 2, 3])

    def test_field_match_handles_missing_nested_and_list_values(self) -> None:
        persona = analytics.FieldMatch.parse("custom_field.cf_persona=analyst")
        self.assertFalse(persona.matches({"custom_field": "flat text"}))
        self.assertFalse(persona.matches({"custom_field": {"cf_persona": None}}))
        tags = analytics.FieldMatch.parse("tags=^vip$")
        self.assertTrue(tags.matches({"tags": ["new", "VIP"]}))
        owner = analytics.FieldMatch.parse("owner=ada")
        self.assertTrue(owner.matches({"owner": {"id": 1, "name": "Ada"}}))
        self.assertTrue(owner.matches({"owner": {"id": 1, "email": "ada@x.example"}}))

    def test_since_and_activity_timestamps(self) -> None:
        self.assertEqual(
            analytics.parse_since("2026-09-01T00:00:00+02:00").utcoffset(),
            timedelta(hours=2),
        )
        self.assertEqual(analytics.parse_since("2026-09-01").tzinfo, UTC)
        filters = analytics.EngagementFilters(since="2026-09-01")
        for created_at, expected in [
            ("2026-09-02T00:00:00", True),  # naive timestamps are treated as UTC
            ("2026-08-31T23:59:59Z", False),
            ("yesterday", False),
            (None, False),
            (1_757_000_000, False),
        ]:
            with self.subTest(created_at=created_at):
                activity = {"action_type": "EMAIL_OPENED", "created_at": created_at}
                self.assertIs(filters.activity_ok(activity), expected)

    def test_company_name_sources(self) -> None:
        for contact, expected in [
            ({"sales_accounts": ["x", {"id": 1}, {"name": "Acme"}]}, "Acme"),
            ({"sales_accounts": [{"id": 1}], "company": {"name": "Beta"}}, "Beta"),
            ({"company": "Gamma"}, "Gamma"),
            ({"company": ""}, None),
            ({"first_name": "Ada", "last_name": "Lovelace"}, None),
        ]:
            with self.subTest(contact=contact):
                summary = analytics._contact_summary(contact)
                self.assertEqual(summary["company"], expected)
        self.assertEqual(
            analytics._contact_summary({"first_name": "Ada", "last_name": "Lovelace"})[
                "display_name"
            ],
            "Ada Lovelace",
        )


class CensusTests(unittest.TestCase):
    def test_counts_types_and_reports_only_key_names(self) -> None:
        client = FakeAudienceClient(
            [CONTACTS],
            {
                1: [opened("2026-09-10T00:00:00Z"), opened("2026-09-11T00:00:00Z")],
                2: [
                    {
                        "action_type": "STAGE_CHANGE",
                        "action_data": {"stage_name": "Won"},
                    }
                ],
            },
        )
        report = analytics.activity_type_census(client, analytics.Audience(list_id="9"))
        self.assertEqual(report["scanned_contacts"], 3)
        self.assertEqual(
            report["action_types"],
            [
                {"action_type": "EMAIL_OPENED", "count": 2,
                 "action_data_keys": ["campaign_name", "subject"]},
                {"action_type": "STAGE_CHANGE", "count": 1,
                 "action_data_keys": ["stage_name"]},
            ],
        )  # fmt: skip
        self.assertNotIn("Product Launch", repr(report))

    def test_census_skips_malformed_entries_and_applies_delay(self) -> None:
        sleeps: list[float] = []
        client = FakeAudienceClient(
            [[{"display_name": "No id"}, *CONTACTS[:2]]],
            {
                1: ["not-an-activity", {"action_type": "NOTE", "action_data": "text"}],
                2: [{"action_data": {"k": 1}}],
            },
        )
        report = analytics.activity_type_census(
            client,
            analytics.Audience(list_id="9"),
            analytics.Limits(max_pages=1, max_contacts=5, delay=0.25),
            sleep=sleeps.append,
        )
        self.assertEqual(sleeps, [0.25])
        self.assertEqual(
            report["action_types"],
            [
                {"action_type": "NOTE", "count": 1, "action_data_keys": []},
                {"action_type": "UNKNOWN", "count": 1, "action_data_keys": ["k"]},
            ],
        )

    def test_census_stops_at_max_contacts(self) -> None:
        client = FakeAudienceClient([CONTACTS], {})
        report = analytics.activity_type_census(
            client,
            analytics.Audience(list_id="9"),
            analytics.Limits(max_pages=1, max_contacts=2),
        )
        self.assertEqual(report["scanned_contacts"], 2)
        self.assertTrue(report["truncated"])
        self.assertEqual(client.timeline_calls, [1, 2])


if __name__ == "__main__":
    unittest.main()
