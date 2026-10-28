"""Read-only campaign analytics assembled from documented GET endpoints.

Freshsales does not publish a campaign-statistics API. What it does publish is
enough to rebuild the useful part of the picture:

* ``GET /lists`` and ``GET /contacts/lists/<id>`` give campaign audiences;
* ``GET /contacts/view/<id>`` gives saved-view audiences;
* ``GET /contacts/<id>/activities`` gives each contact's timeline, which is
  where email events (opens, clicks, ...) are recorded.

The helpers below walk an audience page by page, fetch each contact's
timeline, and keep the activities that match the caller's filters. Every
walk is bounded by explicit page and contact limits so a command never
silently fetches an entire tenant.
"""

from __future__ import annotations

import csv
import io
import json
import re
import time
from collections import Counter
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Protocol

DEFAULT_ACTION_PATTERN = "OPEN"
DEFAULT_MAX_PAGES = 5
DEFAULT_MAX_CONTACTS = 200
DEFAULT_ACTIVITY_LIMIT = 100
CENSUS_MAX_CONTACTS = 25


class AudienceClient(Protocol):
    """The subset of ``FreshsalesClient`` the analytics helpers rely on."""

    def list_contacts(self, view_id: int | str, *, page: int = 1) -> Any: ...

    def marketing_list_contacts(self, list_id: int | str, *, page: int = 1) -> Any: ...

    def contact_activities(
        self,
        contact_id: int | str,
        *,
        include: str | Sequence[str] | None = None,
        limit: int | None = None,
    ) -> Any: ...


@dataclass(frozen=True)
class Audience:
    """Where contacts come from: a marketing list or a saved contact view."""

    list_id: str | None = None
    view_id: str | None = None

    def __post_init__(self) -> None:
        if bool(self.list_id) == bool(self.view_id):
            raise ValueError("exactly one of list_id or view_id is required")

    def describe(self) -> dict[str, str]:
        if self.list_id:
            return {"list_id": str(self.list_id)}
        return {"view_id": str(self.view_id)}

    def fetch_page(self, client: AudienceClient, page: int) -> Any:
        if self.list_id:
            return client.marketing_list_contacts(self.list_id, page=page)
        return client.list_contacts(self.view_id, page=page)


@dataclass(frozen=True)
class FieldMatch:
    """A ``FIELD=REGEX`` contact filter; FIELD may be dotted (``custom_field.cf_x``)."""

    path: str
    pattern: re.Pattern[str]

    @classmethod
    def parse(cls, expression: str) -> FieldMatch:
        name, sep, regex = expression.partition("=")
        name = name.strip()
        if not sep or not name or not regex:
            raise ValueError(f"filter must look like FIELD=REGEX, got {expression!r}")
        return cls(name, _compile(regex, f"filter {name}"))

    def matches(self, contact: Mapping[str, Any]) -> bool:
        value: Any = contact
        for part in self.path.split("."):
            if not isinstance(value, Mapping):
                return False
            value = value.get(part)
        if value is None:
            return False
        values = value if isinstance(value, list) else [value]
        return any(self.pattern.search(_as_text(item)) for item in values)


@dataclass
class EngagementFilters:
    action_pattern: str = DEFAULT_ACTION_PATTERN
    campaign: str | None = None
    since: str | None = None
    contact_filters: Sequence[FieldMatch] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        self._action_re = _compile(self.action_pattern, "action type")
        self._since = parse_since(self.since) if self.since else None
        self._campaign = self.campaign.casefold() if self.campaign else None

    def contact_ok(self, contact: Mapping[str, Any]) -> bool:
        return all(item.matches(contact) for item in self.contact_filters)

    def activity_ok(self, activity: Mapping[str, Any]) -> bool:
        if not self._action_re.search(str(activity.get("action_type") or "")):
            return False
        if self._since is not None:
            created = _parse_timestamp(activity.get("created_at"))
            if created is None or created < self._since:
                return False
        if self._campaign is not None:
            haystack = _as_text(
                {
                    "action_data": activity.get("action_data"),
                    "targetable_name": activity.get("targetable_name"),
                    "targetable_type": activity.get("targetable_type"),
                }
            ).casefold()
            if self._campaign not in haystack:
                return False
        return True

    def describe(self) -> dict[str, Any]:
        return {
            "action_type": self.action_pattern,
            "campaign": self.campaign,
            "since": self.since,
            "contact": [f"{m.path}={m.pattern.pattern}" for m in self.contact_filters],
        }


@dataclass(frozen=True)
class Limits:
    max_pages: int = DEFAULT_MAX_PAGES
    max_contacts: int = DEFAULT_MAX_CONTACTS
    activity_limit: int = DEFAULT_ACTIVITY_LIMIT
    delay: float = 0.0

    def __post_init__(self) -> None:
        if self.max_pages < 1:
            raise ValueError("max_pages must be at least 1")
        if self.max_contacts < 1:
            raise ValueError("max_contacts must be at least 1")
        if self.activity_limit < 1:
            raise ValueError("activity_limit must be at least 1")
        if self.delay < 0:
            raise ValueError("delay must not be negative")


# Audience walking ---------------------------------------------------------
def iter_audience(
    client: AudienceClient, audience: Audience, *, max_pages: int
) -> Iterator[tuple[Mapping[str, Any], bool]]:
    """Yield ``(contact, more_pages_remain)`` for up to ``max_pages`` pages."""

    for page in range(1, max_pages + 1):
        payload = audience.fetch_page(client, page) or {}
        contacts = payload.get("contacts") or []
        total_pages = _total_pages(payload)
        more = bool(contacts) and (total_pages is None or page < total_pages)
        last_page_reached = page == max_pages
        for contact in contacts:
            if isinstance(contact, Mapping):
                yield contact, more and last_page_reached
        if not more:
            return


def _total_pages(payload: Mapping[str, Any]) -> int | None:
    meta = payload.get("meta")
    if isinstance(meta, Mapping):
        try:
            return int(meta.get("total_pages"))
        except (TypeError, ValueError):
            return None
    return None


# Engagement ---------------------------------------------------------------
def campaign_engagement(
    client: AudienceClient,
    audience: Audience,
    filters: EngagementFilters,
    limits: Limits | None = None,
    *,
    sleep: Callable[[float], None] = time.sleep,
) -> dict[str, Any]:
    """Return the audience contacts whose timeline has matching activities."""

    limits = limits or Limits()

    scanned = 0
    eligible = 0
    truncated = False
    type_counts: Counter[str] = Counter()
    engaged: list[dict[str, Any]] = []

    for contact, more_pages in iter_audience(
        client, audience, max_pages=limits.max_pages
    ):
        truncated = truncated or more_pages
        is_eligible = filters.contact_ok(contact)
        # max_contacts caps timeline calls, the expensive part of the walk.
        if is_eligible and eligible >= limits.max_contacts:
            truncated = True
            break
        scanned += 1
        if not is_eligible:
            continue
        eligible += 1

        contact_id = contact.get("id")
        if contact_id is None:
            continue
        if limits.delay and eligible > 1:
            sleep(limits.delay)
        payload = (
            client.contact_activities(contact_id, limit=limits.activity_limit) or {}
        )
        events = [
            _event(activity)
            for activity in payload.get("activities") or []
            if isinstance(activity, Mapping) and filters.activity_ok(activity)
        ]
        if not events:
            continue
        events.sort(key=lambda event: event["created_at"] or "")
        type_counts.update(event["action_type"] for event in events)
        engaged.append(
            {
                **_contact_summary(contact),
                "event_count": len(events),
                "first_event_at": events[0]["created_at"],
                "last_event_at": events[-1]["created_at"],
                "events": events,
            }
        )

    engaged.sort(key=lambda row: row["last_event_at"] or "", reverse=True)
    return {
        "audience": audience.describe(),
        "filters": filters.describe(),
        "scanned_contacts": scanned,
        "eligible_contacts": eligible,
        "engaged_contacts": len(engaged),
        "truncated": truncated,
        "action_type_counts": dict(type_counts.most_common()),
        "contacts": engaged,
    }


def activity_type_census(
    client: AudienceClient,
    audience: Audience,
    limits: Limits | None = None,
    *,
    sleep: Callable[[float], None] = time.sleep,
) -> dict[str, Any]:
    """Count activity types across a small audience sample.

    Freshsales does not document the ``action_type`` names used for campaign
    email events. Run this first to discover them, then pass the right pattern
    to :func:`campaign_engagement`. Only ``action_data`` key names are
    reported, never their values.
    """

    limits = limits or Limits(max_pages=1, max_contacts=CENSUS_MAX_CONTACTS)

    scanned = 0
    truncated = False
    counts: Counter[str] = Counter()
    keys: dict[str, set[str]] = {}
    for contact, more_pages in iter_audience(
        client, audience, max_pages=limits.max_pages
    ):
        truncated = truncated or more_pages
        if scanned >= limits.max_contacts:
            truncated = True
            break
        contact_id = contact.get("id")
        if contact_id is None:
            continue
        if limits.delay and scanned:
            sleep(limits.delay)
        scanned += 1
        payload = (
            client.contact_activities(contact_id, limit=limits.activity_limit) or {}
        )
        for activity in payload.get("activities") or []:
            if not isinstance(activity, Mapping):
                continue
            action_type = str(activity.get("action_type") or "UNKNOWN")
            counts[action_type] += 1
            data = activity.get("action_data")
            if isinstance(data, Mapping):
                keys.setdefault(action_type, set()).update(str(k) for k in data)

    return {
        "audience": audience.describe(),
        "scanned_contacts": scanned,
        "truncated": truncated,
        "action_types": [
            {
                "action_type": name,
                "count": count,
                "action_data_keys": sorted(keys.get(name, ())),
            }
            for name, count in counts.most_common()
        ],
    }


# Output -------------------------------------------------------------------
CSV_COLUMNS = (
    "id",
    "display_name",
    "email",
    "job_title",
    "company",
    "event_count",
    "first_event_at",
    "last_event_at",
    "action_types",
)


def engagement_csv(report: Mapping[str, Any]) -> str:
    """Render one CSV row per engaged contact."""

    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=CSV_COLUMNS, lineterminator="\n")
    writer.writeheader()
    for row in report.get("contacts", []):
        action_types = sorted({event["action_type"] for event in row["events"]})
        writer.writerow(
            {
                **{column: row.get(column) for column in CSV_COLUMNS},
                "action_types": ";".join(action_types),
            }
        )
    return buffer.getvalue()


# Helpers ------------------------------------------------------------------
def parse_since(value: str) -> datetime:
    """Parse ``YYYY-MM-DD`` or an ISO-8601 timestamp; naive values are UTC."""

    try:
        parsed = datetime.fromisoformat(value.strip())
    except ValueError as exc:
        raise ValueError(
            f"since must be an ISO date or timestamp, got {value!r}"
        ) from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed


def _parse_timestamp(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed


def _compile(pattern: str, label: str) -> re.Pattern[str]:
    try:
        return re.compile(pattern, re.IGNORECASE)
    except re.error as exc:
        raise ValueError(f"invalid {label} regex: {exc}") from exc


def _as_text(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, Mapping) and "name" in value and len(value) <= 3:
        return str(value.get("name") or "")
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)


def _event(activity: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "action_type": str(activity.get("action_type") or "UNKNOWN"),
        "created_at": activity.get("created_at"),
        "targetable_type": activity.get("targetable_type"),
        "targetable_name": activity.get("targetable_name"),
        "action_data": activity.get("action_data"),
    }


def _contact_summary(contact: Mapping[str, Any]) -> dict[str, Any]:
    name = contact.get("display_name") or " ".join(
        part for part in (contact.get("first_name"), contact.get("last_name")) if part
    )
    return {
        "id": contact.get("id"),
        "display_name": name or None,
        "email": contact.get("email"),
        "job_title": contact.get("job_title"),
        "company": _company_name(contact),
    }


def _company_name(contact: Mapping[str, Any]) -> str | None:
    accounts = contact.get("sales_accounts")
    if isinstance(accounts, list):
        for account in accounts:
            if isinstance(account, Mapping) and account.get("name"):
                return str(account["name"])
    company = contact.get("company")
    if isinstance(company, Mapping) and company.get("name"):
        return str(company["name"])
    if isinstance(company, str) and company:
        return company
    return None
