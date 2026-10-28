"""Small, dependency-free Freshsales API client.

The public methods intentionally return the API's JSON dictionaries unchanged.
Freshsales accounts often contain custom fields, so preserving unknown fields is
more useful here than imposing a lossy fixed schema.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Protocol
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode, urlsplit
from urllib.request import Request, urlopen

from . import __version__
from .errors import ApiError, NetworkError, ResponseDecodeError, WriteDisabledError

# Environment variable the CLI reads the API base URL from, e.g.
# https://<domain>.myfreshworks.com/crm/sales/api
BASE_URL_ENV = "FRESHSALES_BASE_URL"
DEFAULT_TIMEOUT = 30.0
MAX_LIST_NAME_LENGTH = 255
# A LinkedIn vanity alias: letters, digits, hyphens and underscores, plus
# percent-encoded characters for non-ASCII names. Slashes, dots and "://"
# are rejected so a URL can never be stored by mistake.
LINKEDIN_ALIAS_PATTERN = re.compile(
    r"[A-Za-z0-9](?:[A-Za-z0-9_-]|%[0-9A-Fa-f]{2}){1,99}"
)


TASK_FILTERS = ("open", "due_today", "due_tomorrow", "overdue", "completed")
APPOINTMENT_FILTERS = ("upcoming", "past")

# CLI entity names for `fields`, mapped to their /settings/<name>/fields path.
FIELD_ENTITIES = {
    "contacts": "contacts",
    "accounts": "sales_accounts",
    "deals": "deals",
    "sales-activities": "sales_activities",
}


@dataclass(frozen=True)
class WriteRequest:
    """A planned mutating request, built without touching the network.

    The CLI prints these for dry runs and passes the same objects to
    :meth:`FreshsalesClient.send` when the user applies them, so what is
    previewed is exactly what is sent.
    """

    method: str
    path: str
    body: Mapping[str, Any]
    summary: str

    def describe(self, base_url: str) -> dict[str, Any]:
        return {
            "method": self.method,
            "url": f"{base_url.rstrip('/')}{self.path}",
            "body": dict(self.body),
            "summary": self.summary,
        }


@dataclass(frozen=True)
class HttpResponse:
    """Minimal response object shared by the real and fake transports."""

    status_code: int
    body: bytes
    headers: Mapping[str, str]


class Transport(Protocol):
    """Transport contract used to keep API tests entirely offline."""

    def request(
        self,
        method: str,
        url: str,
        *,
        headers: Mapping[str, str],
        body: bytes | None,
        timeout: float,
    ) -> HttpResponse: ...


class UrllibTransport:
    """HTTPS transport implemented with Python's standard library."""

    def request(
        self,
        method: str,
        url: str,
        *,
        headers: Mapping[str, str],
        body: bytes | None,
        timeout: float,
    ) -> HttpResponse:
        request = Request(url, data=body, headers=dict(headers), method=method)
        try:
            with urlopen(request, timeout=timeout) as response:
                return HttpResponse(
                    status_code=int(response.getcode()),
                    body=response.read(),
                    headers={key: value for key, value in response.headers.items()},
                )
        except HTTPError as exc:
            # HTTPError is also a file-like response. Reading it lets the
            # client expose the API's structured error message without making
            # a second request; closing it afterwards frees the connection.
            with exc:
                return HttpResponse(
                    status_code=exc.code,
                    body=exc.read(),
                    headers={key: value for key, value in exc.headers.items()},
                )
        except (URLError, TimeoutError, OSError) as exc:
            raise NetworkError("could not reach the Freshsales API") from exc


class FreshsalesClient:
    """Freshsales API operations used by the CLI and unit tests."""

    def __init__(
        self,
        api_key: str,
        *,
        base_url: str,
        timeout: float = DEFAULT_TIMEOUT,
        transport: Transport | None = None,
        allow_writes: bool = False,
    ) -> None:
        if not api_key or not api_key.strip():
            raise ValueError("api_key must not be empty")
        if timeout <= 0:
            raise ValueError("timeout must be greater than zero")

        parsed = urlsplit(base_url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("base_url must be an absolute http(s) URL")

        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.timeout = float(timeout)
        self.transport = transport or UrllibTransport()
        self.allow_writes = allow_writes

    def _request(
        self,
        method: str,
        path: str,
        *,
        params: Mapping[str, Any] | None = None,
        body: Mapping[str, Any] | None = None,
    ) -> Any:
        method = method.upper()
        if method != "GET" and not self.allow_writes:
            raise WriteDisabledError()
        if not path.startswith("/"):
            raise ValueError("API paths must start with '/'")

        url = f"{self.base_url}{path}"
        if params:
            query = [(key, value) for key, value in params.items() if value is not None]
            if query:
                url = f"{url}?{urlencode(query, doseq=True)}"

        encoded_body: bytes | None = None
        if body is not None:
            encoded_body = json.dumps(body, ensure_ascii=False).encode("utf-8")

        response = self.transport.request(
            method,
            url,
            headers={
                "Accept": "application/json",
                "Authorization": f"Token token={self.api_key}",
                "Content-Type": "application/json",
                "User-Agent": f"freshsales-cli/{__version__}",
            },
            body=encoded_body,
            timeout=self.timeout,
        )

        if not 200 <= response.status_code < 300:
            raise ApiError(response.status_code, _error_message(response.body))
        if not response.body:
            return None

        try:
            return json.loads(response.body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ResponseDecodeError("Freshsales returned invalid JSON") from exc

    @staticmethod
    def _id(value: int | str) -> str:
        value_as_text = str(value).strip()
        if not value_as_text:
            raise ValueError("resource id must not be empty")
        return quote(value_as_text, safe="")

    @staticmethod
    def _include(value: str | Sequence[str] | None) -> str | None:
        if value is None:
            return None
        if isinstance(value, str):
            items = [item.strip() for item in value.split(",")]
        else:
            items = [str(item).strip() for item in value]
        items = [item for item in items if item]
        return ",".join(items) or None

    @staticmethod
    def _required_text(value: str, label: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError(f"{label} must not be empty")
        return value

    @staticmethod
    def _page(page: int) -> int:
        if page < 1:
            raise ValueError("page must be at least 1")
        return page

    @staticmethod
    def _per_page(per_page: int) -> int:
        if not 1 <= per_page <= 100:
            raise ValueError("per_page must be between 1 and 100")
        return per_page

    def _list_params(
        self,
        *,
        page: int,
        sort: str | None,
        sort_type: str | None,
        include: str | Sequence[str] | None,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {"page": self._page(page)}
        if sort:
            params["sort"] = sort
        if sort_type:
            params["sort_type"] = sort_type
        include_value = self._include(include)
        if include_value:
            params["include"] = include_value
        return params

    # Contacts -----------------------------------------------------------
    def get_contact(
        self, contact_id: int | str, *, include: str | Sequence[str] | None = None
    ) -> Any:
        return self._request(
            "GET",
            f"/contacts/{self._id(contact_id)}",
            params={"include": self._include(include)},
        )

    def contact_filters(self) -> Any:
        return self._request("GET", "/contacts/filters")

    def list_contacts(
        self,
        view_id: int | str,
        *,
        page: int = 1,
        sort: str | None = None,
        sort_type: str | None = None,
        include: str | Sequence[str] | None = None,
    ) -> Any:
        return self._request(
            "GET",
            f"/contacts/view/{self._id(view_id)}",
            params=self._list_params(
                page=page,
                sort=sort,
                sort_type=sort_type,
                include=include,
            ),
        )

    # Accounts -----------------------------------------------------------
    def get_account(
        self, account_id: int | str, *, include: str | Sequence[str] | None = None
    ) -> Any:
        return self._request(
            "GET",
            f"/sales_accounts/{self._id(account_id)}",
            params={"include": self._include(include)},
        )

    def account_filters(self) -> Any:
        return self._request("GET", "/sales_accounts/filters")

    def list_accounts(
        self,
        view_id: int | str,
        *,
        page: int = 1,
        sort: str | None = None,
        sort_type: str | None = None,
        include: str | Sequence[str] | None = None,
    ) -> Any:
        return self._request(
            "GET",
            f"/sales_accounts/view/{self._id(view_id)}",
            params=self._list_params(
                page=page,
                sort=sort,
                sort_type=sort_type,
                include=include,
            ),
        )

    # Deals --------------------------------------------------------------
    def get_deal(
        self, deal_id: int | str, *, include: str | Sequence[str] | None = None
    ) -> Any:
        return self._request(
            "GET",
            f"/deals/{self._id(deal_id)}",
            params={"include": self._include(include)},
        )

    def deal_filters(self) -> Any:
        return self._request("GET", "/deals/filters")

    def list_deals(
        self,
        view_id: int | str,
        *,
        page: int = 1,
        sort: str | None = None,
        sort_type: str | None = None,
        include: str | Sequence[str] | None = None,
    ) -> Any:
        return self._request(
            "GET",
            f"/deals/view/{self._id(view_id)}",
            params=self._list_params(
                page=page,
                sort=sort,
                sort_type=sort_type,
                include=include,
            ),
        )

    # Search -------------------------------------------------------------
    def search(
        self,
        query: str,
        *,
        include: str | Sequence[str],
        per_page: int = 10,
    ) -> Any:
        query = self._required_text(query, "query")
        include_value = self._include(include)
        if not include_value:
            raise ValueError("include must contain at least one entity")
        return self._request(
            "GET",
            "/search",
            params={
                "q": query,
                "include": include_value,
                "per_page": self._per_page(per_page),
            },
        )

    def lookup(
        self,
        query: str,
        *,
        field: str,
        entities: str | Sequence[str],
    ) -> Any:
        query = self._required_text(query, "query")
        field = self._required_text(field, "field")
        entities_value = self._include(entities)
        if not entities_value:
            raise ValueError("entities must contain at least one entity")
        return self._request(
            "GET",
            "/lookup",
            params={"q": query, "f": field, "entities": entities_value},
        )

    # Tasks and appointments --------------------------------------------
    @staticmethod
    def _choice(value: str, choices: Sequence[str], label: str) -> str:
        if value not in choices:
            raise ValueError(f"{label} must be one of: {', '.join(choices)}")
        return value

    def list_tasks(
        self,
        *,
        filter_name: str = "open",
        page: int = 1,
        include: str | Sequence[str] | None = None,
    ) -> Any:
        return self._request(
            "GET",
            "/tasks",
            params={
                "filter": self._choice(filter_name, TASK_FILTERS, "task filter"),
                "page": self._page(page),
                "include": self._include(include),
            },
        )

    def get_task(
        self, task_id: int | str, *, include: str | Sequence[str] | None = None
    ) -> Any:
        return self._request(
            "GET",
            f"/tasks/{self._id(task_id)}",
            params={"include": self._include(include)},
        )

    def list_appointments(
        self,
        *,
        filter_name: str = "upcoming",
        page: int = 1,
        include: str | Sequence[str] | None = None,
    ) -> Any:
        return self._request(
            "GET",
            "/appointments",
            params={
                "filter": self._choice(
                    filter_name, APPOINTMENT_FILTERS, "appointment filter"
                ),
                "page": self._page(page),
                "include": self._include(include),
            },
        )

    def get_appointment(
        self, appointment_id: int | str, *, include: str | Sequence[str] | None = None
    ) -> Any:
        return self._request(
            "GET",
            f"/appointments/{self._id(appointment_id)}",
            params={"include": self._include(include)},
        )

    # Field metadata -----------------------------------------------------
    def fields(self, entity: str, *, include: str | Sequence[str] | None = None) -> Any:
        """Return the field definitions (labels, types, choices) of an entity.

        ``entity`` is a CLI name from ``FIELD_ENTITIES``; it maps to
        ``/settings/<api name>/fields``.
        """

        if entity not in FIELD_ENTITIES:
            choices = ", ".join(FIELD_ENTITIES)
            raise ValueError(f"field entity must be one of: {choices}")
        return self._request(
            "GET",
            f"/settings/{FIELD_ENTITIES[entity]}/fields",
            params={"include": self._include(include)},
        )

    # Campaigns, marketing lists and activities -------------------------
    def campaigns(self) -> Any:
        """Return the id/name pairs of every campaign (``/selector/campaigns``)."""

        return self._request("GET", "/selector/campaigns")

    def selector(self, name: str) -> Any:
        """Return a documented ``/selector/<name>`` lookup table."""

        name = self._required_text(name, "selector name")
        return self._request("GET", f"/selector/{self._id(name)}")

    def marketing_lists(self, *, page: int = 1) -> Any:
        return self._request("GET", "/lists", params={"page": self._page(page)})

    def marketing_list_contacts(self, list_id: int | str, *, page: int = 1) -> Any:
        return self._request(
            "GET",
            f"/contacts/lists/{self._id(list_id)}",
            params={"page": self._page(page)},
        )

    def contact_activities(
        self,
        contact_id: int | str,
        *,
        include: str | Sequence[str] | None = None,
        limit: int | None = None,
    ) -> Any:
        if limit is not None and limit < 1:
            raise ValueError("limit must be at least 1")
        return self._request(
            "GET",
            f"/contacts/{self._id(contact_id)}/activities",
            params={"include": self._include(include), "limit": limit},
        )

    # Marketing-list writes ---------------------------------------------
    # The plan_* static methods validate input and return WriteRequest
    # objects without network access; ``send`` executes one and still
    # requires allow_writes=True. Freshsales has no list-delete endpoint, and
    # none is exposed here.
    @staticmethod
    def _list_name(name: str) -> str:
        name = FreshsalesClient._required_text(name, "list name")
        if len(name) > MAX_LIST_NAME_LENGTH:
            raise ValueError(
                f"list name must be at most {MAX_LIST_NAME_LENGTH} characters"
            )
        return name

    @staticmethod
    def _contact_ids(ids: Sequence[int | str]) -> list[int]:
        cleaned: list[int] = []
        seen: set[int] = set()
        for value in ids:
            text = str(value).strip()
            if not text.isdigit() or int(text) < 1:
                raise ValueError(f"contact id must be a positive integer: {value!r}")
            number = int(text)
            if number not in seen:
                seen.add(number)
                cleaned.append(number)
        if not cleaned:
            raise ValueError("at least one contact id is required")
        return cleaned

    @staticmethod
    def _list_id(value: int | str, label: str = "list id") -> int:
        text = str(value).strip()
        if not text.isdigit() or int(text) < 1:
            raise ValueError(f"{label} must be a positive integer: {value!r}")
        return int(text)

    @staticmethod
    def plan_create_list(name: str) -> WriteRequest:
        name = FreshsalesClient._list_name(name)
        return WriteRequest(
            "POST", "/lists", {"name": name}, f"create marketing list {name!r}"
        )

    @staticmethod
    def plan_rename_list(list_id: int | str, name: str) -> WriteRequest:
        target = FreshsalesClient._list_id(list_id)
        name = FreshsalesClient._list_name(name)
        return WriteRequest(
            "PUT",
            f"/lists/{target}",
            {"name": name},
            f"rename marketing list {target} to {name!r}",
        )

    @staticmethod
    def plan_add_contacts(
        list_id: int | str, contact_ids: Sequence[int | str]
    ) -> WriteRequest:
        target = FreshsalesClient._list_id(list_id)
        ids = FreshsalesClient._contact_ids(contact_ids)
        return WriteRequest(
            "PUT",
            f"/lists/{target}/add_contacts",
            {"ids": ids},
            f"add {len(ids)} contact(s) to list {target}",
        )

    @staticmethod
    def plan_remove_contacts(
        list_id: int | str,
        contact_ids: Sequence[int | str] | None = None,
        *,
        remove_all: bool = False,
    ) -> WriteRequest:
        target = FreshsalesClient._list_id(list_id)
        if remove_all:
            if contact_ids:
                raise ValueError("pass contact ids or remove_all, not both")
            return WriteRequest(
                "PUT",
                f"/lists/{target}/remove_contacts",
                {"all": True},
                f"remove ALL contacts from list {target}",
            )
        ids = FreshsalesClient._contact_ids(contact_ids or [])
        return WriteRequest(
            "PUT",
            f"/lists/{target}/remove_contacts",
            {"ids": ids},
            f"remove {len(ids)} contact(s) from list {target}",
        )

    @staticmethod
    def plan_move_contacts(
        list_id: int | str,
        from_list_id: int | str,
        contact_ids: Sequence[int | str] | None = None,
        *,
        move_all: bool = False,
    ) -> WriteRequest:
        target = FreshsalesClient._list_id(list_id)
        source = FreshsalesClient._list_id(from_list_id, "from list id")
        if source == target:
            raise ValueError("source and destination lists must differ")
        if move_all:
            if contact_ids:
                raise ValueError("pass contact ids or move_all, not both")
            return WriteRequest(
                "PUT",
                f"/lists/{target}/move_contacts",
                {"from_list_id": source},
                f"move ALL contacts from list {source} to list {target}",
            )
        ids = FreshsalesClient._contact_ids(contact_ids or [])
        return WriteRequest(
            "PUT",
            f"/lists/{target}/move_contacts",
            {"ids": ids, "from_list_id": source},
            f"move {len(ids)} contact(s) from list {source} to list {target}",
        )

    # Contact LinkedIn writes -------------------------------------------
    # The only contact field the CLI can change. The body never carries
    # any other field, so the rest of the record is left untouched.
    @staticmethod
    def _linkedin_alias(value: str) -> str:
        alias = str(value or "").strip()
        if not LINKEDIN_ALIAS_PATTERN.fullmatch(alias):
            raise ValueError(
                "linkedin must be a profile alias (the part after "
                f"linkedin.com/in/), not a URL: {value!r}"
            )
        return alias

    @staticmethod
    def plan_set_contact_linkedin(contact_id: int | str, alias: str) -> WriteRequest:
        target = FreshsalesClient._list_id(contact_id, "contact id")
        alias = FreshsalesClient._linkedin_alias(alias)
        return WriteRequest(
            "PUT",
            f"/contacts/{target}",
            {"contact": {"linkedin": alias}},
            f"set linkedin of contact {target} to {alias!r}",
        )

    def send(self, request: WriteRequest) -> Any:
        """Execute a planned write; raises WriteDisabledError unless enabled."""

        return self._request(request.method, request.path, body=request.body)

    def create_list(self, name: str) -> Any:
        return self.send(self.plan_create_list(name))

    def rename_list(self, list_id: int | str, name: str) -> Any:
        return self.send(self.plan_rename_list(list_id, name))

    def add_contacts_to_list(
        self, list_id: int | str, contact_ids: Sequence[int | str]
    ) -> Any:
        return self.send(self.plan_add_contacts(list_id, contact_ids))

    def remove_contacts_from_list(
        self,
        list_id: int | str,
        contact_ids: Sequence[int | str] | None = None,
        *,
        remove_all: bool = False,
    ) -> Any:
        return self.send(
            self.plan_remove_contacts(list_id, contact_ids, remove_all=remove_all)
        )

    def move_contacts_between_lists(
        self,
        list_id: int | str,
        from_list_id: int | str,
        contact_ids: Sequence[int | str] | None = None,
        *,
        move_all: bool = False,
    ) -> Any:
        return self.send(
            self.plan_move_contacts(
                list_id, from_list_id, contact_ids, move_all=move_all
            )
        )

    # Write request builders --------------------------------------------
    # These methods are deliberately unavailable unless the caller opts in
    # with allow_writes=True. The initial CLI never opts in; unit tests use a
    # recording fake transport to verify payloads without touching production.
    def _write_entity(
        self,
        method: str,
        path: str,
        envelope: str,
        data: Mapping[str, Any],
    ) -> Any:
        if not isinstance(data, Mapping):
            raise TypeError("entity data must be a mapping")
        return self._request(method, path, body={envelope: dict(data)})

    def _upsert_entity(
        self,
        path: str,
        envelope: str,
        unique_identifier: Mapping[str, Any],
        data: Mapping[str, Any],
    ) -> Any:
        if not isinstance(unique_identifier, Mapping):
            raise TypeError("unique_identifier must be a mapping")
        if not isinstance(data, Mapping):
            raise TypeError("entity data must be a mapping")
        return self._request(
            "POST",
            path,
            body={
                "unique_identifier": dict(unique_identifier),
                envelope: dict(data),
            },
        )

    def create_contact(self, data: Mapping[str, Any]) -> Any:
        return self._write_entity("POST", "/contacts", "contact", data)

    def update_contact(self, contact_id: int | str, data: Mapping[str, Any]) -> Any:
        return self._write_entity(
            "PUT", f"/contacts/{self._id(contact_id)}", "contact", data
        )

    def upsert_contact(
        self,
        unique_identifier: Mapping[str, Any],
        data: Mapping[str, Any],
    ) -> Any:
        return self._upsert_entity(
            "/contacts/upsert", "contact", unique_identifier, data
        )

    def create_account(self, data: Mapping[str, Any]) -> Any:
        return self._write_entity("POST", "/sales_accounts", "sales_account", data)

    def update_account(self, account_id: int | str, data: Mapping[str, Any]) -> Any:
        return self._write_entity(
            "PUT", f"/sales_accounts/{self._id(account_id)}", "sales_account", data
        )

    def upsert_account(
        self,
        unique_identifier: Mapping[str, Any],
        data: Mapping[str, Any],
    ) -> Any:
        return self._upsert_entity(
            "/sales_accounts/upsert", "sales_account", unique_identifier, data
        )

    def create_deal(self, data: Mapping[str, Any]) -> Any:
        return self._write_entity("POST", "/deals", "deal", data)

    def update_deal(self, deal_id: int | str, data: Mapping[str, Any]) -> Any:
        return self._write_entity("PUT", f"/deals/{self._id(deal_id)}", "deal", data)

    def upsert_deal(
        self,
        unique_identifier: Mapping[str, Any],
        data: Mapping[str, Any],
    ) -> Any:
        return self._upsert_entity("/deals/upsert", "deal", unique_identifier, data)


def _error_message(body: bytes) -> str:
    """Extract a safe, short API message without dumping response data."""

    try:
        payload = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return "request failed"

    if isinstance(payload, Mapping):
        errors = payload.get("errors")
        if isinstance(errors, Mapping) and errors.get("message"):
            return str(errors["message"])
        if payload.get("message"):
            return str(payload["message"])
    return "request failed"
