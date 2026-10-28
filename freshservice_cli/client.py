"""Small, dependency-free Freshservice API client.

The public methods intentionally return the API's JSON dictionaries unchanged.
Freshservice accounts can contain custom fields, so preserving unknown fields
is more useful here than imposing a lossy fixed schema.
"""

from __future__ import annotations

import base64
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Protocol
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode, urlsplit
from urllib.request import Request, urlopen

from . import __version__
from .errors import ApiError, NetworkError, ResponseDecodeError, WriteDisabledError

# Environment variable the CLI reads the API base URL from, e.g.
# https://<domain>.freshservice.com/api/v2
BASE_URL_ENV = "FRESHSERVICE_BASE_URL"
DEFAULT_TIMEOUT = 30.0
ANNOUNCEMENT_STATES = ("active", "archived", "scheduled")


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
            # client expose the API's structured error message without a
            # second request; closing it afterwards frees the connection.
            with exc:
                return HttpResponse(
                    status_code=exc.code,
                    body=exc.read(),
                    headers={key: value for key, value in exc.headers.items()},
                )
        except (URLError, TimeoutError, OSError) as exc:
            raise NetworkError("could not reach the Freshservice API") from exc


class FreshserviceClient:
    """Freshservice operations used by the read-only CLI."""

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

        basic_token = base64.b64encode(f"{self.api_key}:X".encode()).decode("ascii")
        response = self.transport.request(
            method,
            url,
            headers={
                "Accept": "application/json",
                "Authorization": f"Basic {basic_token}",
                "Content-Type": "application/json",
                "User-Agent": f"freshservice-cli/{__version__}",
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
            raise ResponseDecodeError("Freshservice returned invalid JSON") from exc

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

    def _pagination(self, *, page: int, per_page: int) -> dict[str, Any]:
        return {"page": self._page(page), "per_page": self._per_page(per_page)}

    # Tickets -----------------------------------------------------------
    def get_ticket(
        self, ticket_id: int | str, *, include: str | Sequence[str] | None = None
    ) -> Any:
        return self._request(
            "GET",
            f"/tickets/{self._id(ticket_id)}",
            params={"include": self._include(include)},
        )

    def list_tickets(
        self,
        *,
        page: int = 1,
        per_page: int = 30,
        include: str | Sequence[str] | None = None,
        order_type: str | None = None,
        filter_name: str | None = None,
        requester_id: int | str | None = None,
        email: str | None = None,
        updated_since: str | None = None,
        ticket_type: str | None = None,
        workspace_id: int | str | None = None,
    ) -> Any:
        params = self._pagination(page=page, per_page=per_page)
        params.update(
            {
                "include": self._include(include),
                "order_type": order_type,
                "filter": filter_name,
                "requester_id": requester_id,
                "email": email,
                "updated_since": updated_since,
                "type": ticket_type,
                "workspace_id": workspace_id,
            }
        )
        return self._request("GET", "/tickets", params=params)

    def filter_tickets(
        self,
        query: str,
        *,
        page: int = 1,
        per_page: int = 30,
        workspace_id: int | str | None = None,
    ) -> Any:
        query = self._required_text(query, "query")
        params = self._pagination(page=page, per_page=per_page)
        params.update({"query": query, "workspace_id": workspace_id})
        return self._request("GET", "/tickets/filter", params=params)

    def ticket_conversations(
        self, ticket_id: int | str, *, page: int = 1, per_page: int = 30
    ) -> Any:
        """Return a ticket's replies and notes, one page at a time."""

        return self._request(
            "GET",
            f"/tickets/{self._id(ticket_id)}/conversations",
            params=self._pagination(page=page, per_page=per_page),
        )

    def ticket_tasks(
        self, ticket_id: int | str, *, page: int = 1, per_page: int = 30
    ) -> Any:
        return self._request(
            "GET",
            f"/tickets/{self._id(ticket_id)}/tasks",
            params=self._pagination(page=page, per_page=per_page),
        )

    def ticket_approvals(self, ticket_id: int | str) -> Any:
        return self._request("GET", f"/tickets/{self._id(ticket_id)}/approvals")

    def ticket_requested_items(self, ticket_id: int | str) -> Any:
        """Return the service-catalog items requested by a service request."""

        return self._request("GET", f"/tickets/{self._id(ticket_id)}/requested_items")

    def ticket_fields(self, *, workspace_id: int | str | None = None) -> Any:
        """Return ticket form fields: labels, types and choice IDs."""

        return self._request(
            "GET", "/ticket_form_fields", params={"workspace_id": workspace_id}
        )

    def send(self, request: Any) -> Any:
        """Execute a planned ``writes.WriteRequest``.

        Raises WriteDisabledError unless the client allows writes.
        """

        return self._request(request.method, request.path, body=request.body)

    # Requesters / contacts --------------------------------------------
    def get_requester(self, requester_id: int | str) -> Any:
        return self._request("GET", f"/requesters/{self._id(requester_id)}")

    def list_requesters(
        self,
        *,
        page: int = 1,
        per_page: int = 30,
        email: str | None = None,
        mobile_phone_number: str | None = None,
        work_phone_number: str | None = None,
        include_agents: bool = False,
    ) -> Any:
        params = self._pagination(page=page, per_page=per_page)
        params.update(
            {
                "email": email,
                "mobile_phone_number": mobile_phone_number,
                "work_phone_number": work_phone_number,
                "include_agents": "true" if include_agents else None,
            }
        )
        return self._request("GET", "/requesters", params=params)

    # Agents ------------------------------------------------------------
    def get_current_agent(self) -> Any:
        """Return the agent that owns the API key; needs no agent-admin role."""

        return self._request("GET", "/agents/me")

    def get_agent(self, agent_id: int | str) -> Any:
        return self._request("GET", f"/agents/{self._id(agent_id)}")

    def list_agents(
        self,
        *,
        page: int = 1,
        per_page: int = 30,
        query: str | None = None,
        workspace_id: int | str | None = None,
    ) -> Any:
        params = self._pagination(page=page, per_page=per_page)
        params.update({"query": query, "workspace_id": workspace_id})
        return self._request("GET", "/agents", params=params)

    # Departments (shown as "companies" in MSP mode) -------------------
    # Freshservice has no /companies route; departments are the API resource.
    def get_department(self, department_id: int | str) -> Any:
        return self._request("GET", f"/departments/{self._id(department_id)}")

    def list_departments(
        self,
        *,
        page: int = 1,
        per_page: int = 30,
        query: str | None = None,
        workspace_id: int | str | None = None,
    ) -> Any:
        params = self._pagination(page=page, per_page=per_page)
        params.update({"query": query, "workspace_id": workspace_id})
        return self._request("GET", "/departments", params=params)

    # Groups, workspaces and roles -------------------------------------
    def get_group(self, group_id: int | str) -> Any:
        return self._request("GET", f"/groups/{self._id(group_id)}")

    def list_groups(self, *, page: int = 1, per_page: int = 30) -> Any:
        return self._request(
            "GET", "/groups", params=self._pagination(page=page, per_page=per_page)
        )

    def list_requester_groups(self, *, page: int = 1, per_page: int = 30) -> Any:
        # Only the list is exposed: requester-group detail and members need an
        # admin role the read-only key does not have.
        return self._request(
            "GET",
            "/requester_groups",
            params=self._pagination(page=page, per_page=per_page),
        )

    def get_workspace(self, workspace_id: int | str) -> Any:
        return self._request("GET", f"/workspaces/{self._id(workspace_id)}")

    def list_workspaces(self, *, page: int = 1, per_page: int = 30) -> Any:
        return self._request(
            "GET", "/workspaces", params=self._pagination(page=page, per_page=per_page)
        )

    def get_role(self, role_id: int | str) -> Any:
        return self._request("GET", f"/roles/{self._id(role_id)}")

    def list_roles(self, *, page: int = 1, per_page: int = 30) -> Any:
        return self._request(
            "GET", "/roles", params=self._pagination(page=page, per_page=per_page)
        )

    # Announcements and canned responses -------------------------------
    def get_announcement(self, announcement_id: int | str) -> Any:
        return self._request("GET", f"/announcements/{self._id(announcement_id)}")

    def list_announcements(
        self, *, page: int = 1, per_page: int = 30, state: str | None = None
    ) -> Any:
        if state is not None and state not in ANNOUNCEMENT_STATES:
            choices = ", ".join(ANNOUNCEMENT_STATES)
            raise ValueError(f"announcement state must be one of: {choices}")
        params = self._pagination(page=page, per_page=per_page)
        params["state"] = state
        return self._request("GET", "/announcements", params=params)

    def get_canned_response(self, response_id: int | str) -> Any:
        return self._request("GET", f"/canned_responses/{self._id(response_id)}")

    def list_canned_responses(self, *, page: int = 1, per_page: int = 30) -> Any:
        return self._request(
            "GET",
            "/canned_responses",
            params=self._pagination(page=page, per_page=per_page),
        )

    def list_canned_response_folders(self) -> Any:
        return self._request("GET", "/canned_response_folders")

    # Solutions (knowledge base) ----------------------------------------
    def list_solution_categories(
        self,
        *,
        page: int = 1,
        per_page: int = 30,
        workspace_id: int | str | None = None,
    ) -> Any:
        params = self._pagination(page=page, per_page=per_page)
        params["workspace_id"] = workspace_id
        return self._request("GET", "/solutions/categories", params=params)

    def get_solution_category(self, category_id: int | str) -> Any:
        return self._request("GET", f"/solutions/categories/{self._id(category_id)}")

    def list_solution_folders(
        self, category_id: int | str, *, page: int = 1, per_page: int = 30
    ) -> Any:
        params = self._pagination(page=page, per_page=per_page)
        params["category_id"] = self._required_text(str(category_id), "category id")
        return self._request("GET", "/solutions/folders", params=params)

    def get_solution_folder(self, folder_id: int | str) -> Any:
        return self._request("GET", f"/solutions/folders/{self._id(folder_id)}")

    def list_solution_articles(
        self, folder_id: int | str, *, page: int = 1, per_page: int = 30
    ) -> Any:
        params = self._pagination(page=page, per_page=per_page)
        params["folder_id"] = self._required_text(str(folder_id), "folder id")
        return self._request("GET", "/solutions/articles", params=params)

    def get_solution_article(self, article_id: int | str) -> Any:
        return self._request("GET", f"/solutions/articles/{self._id(article_id)}")

    def search_solution_articles(
        self, term: str, *, page: int = 1, per_page: int = 30
    ) -> Any:
        params = self._pagination(page=page, per_page=per_page)
        params["search_term"] = self._required_text(term, "search term")
        return self._request("GET", "/solutions/articles/search", params=params)


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
        if isinstance(errors, list) and errors:
            first = errors[0]
            if isinstance(first, Mapping) and first.get("message"):
                return str(first["message"])
        for key in ("message", "description"):
            if payload.get(key):
                return str(payload[key])
    return "request failed"
