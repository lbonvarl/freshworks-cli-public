from __future__ import annotations

import base64
import io
import unittest
from email.message import Message
from unittest import mock
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlsplit

import freshservice_cli
from freshservice_cli.client import FreshserviceClient, HttpResponse, UrllibTransport
from freshservice_cli.errors import (
    ApiError,
    NetworkError,
    ResponseDecodeError,
    WriteDisabledError,
)

MODULE = "freshservice_cli.client"


class RecordingTransport:
    def __init__(
        self, *, body: bytes = b'{"ok": true}', status_code: int = 200
    ) -> None:
        self.body = body
        self.status_code = status_code
        self.requests: list[dict[str, object]] = []

    def request(
        self,
        method: str,
        url: str,
        *,
        headers: dict[str, str] | object,
        body: bytes | None,
        timeout: float,
    ) -> HttpResponse:
        self.requests.append(
            {
                "method": method,
                "url": url,
                "headers": headers,
                "body": body,
                "timeout": timeout,
            }
        )
        return HttpResponse(self.status_code, self.body, {})


def make_client(
    transport: RecordingTransport, *, allow_writes: bool = False
) -> FreshserviceClient:
    return FreshserviceClient(
        "test-api-key",
        base_url="https://example.test/api/v2",
        transport=transport,
        timeout=7,
        allow_writes=allow_writes,
    )


class FreshserviceReadRequestTests(unittest.TestCase):
    def test_detail_request_uses_basic_auth_and_encodes_id(self) -> None:
        transport = RecordingTransport(body=b'{"ticket": {"id": 42}}')
        result = make_client(transport).get_ticket(
            "a/b", include=["requester", "stats"]
        )

        self.assertEqual(result, {"ticket": {"id": 42}})
        request = transport.requests[0]
        self.assertEqual(request["method"], "GET")
        self.assertEqual(
            request["url"],
            "https://example.test/api/v2/tickets/a%2Fb?include=requester%2Cstats",
        )
        headers = request["headers"]
        self.assertIsInstance(headers, dict)
        expected = base64.b64encode(b"test-api-key:X").decode("ascii")
        self.assertEqual(headers["Authorization"], f"Basic {expected}")
        self.assertEqual(headers["Accept"], "application/json")
        self.assertIsNone(request["body"])

    def test_ticket_list_and_filter_routes(self) -> None:
        transport = RecordingTransport()
        client = make_client(transport)

        client.list_tickets(
            page=2,
            per_page=50,
            include="requester",
            order_type="asc",
            filter_name="watching",
            requester_id=9,
            email="person@example.com",
            updated_since="2026-01-01T00:00:00Z",
            ticket_type="Incident",
            workspace_id=3,
        )
        client.filter_tickets("priority:3 AND status:2", page=3, per_page=10)

        list_query = parse_qs(urlsplit(transport.requests[0]["url"]).query)
        filter_query = parse_qs(urlsplit(transport.requests[1]["url"]).query)
        self.assertEqual(
            list_query,
            {
                "page": ["2"],
                "per_page": ["50"],
                "include": ["requester"],
                "order_type": ["asc"],
                "filter": ["watching"],
                "requester_id": ["9"],
                "email": ["person@example.com"],
                "updated_since": ["2026-01-01T00:00:00Z"],
                "type": ["Incident"],
                "workspace_id": ["3"],
            },
        )
        self.assertEqual(
            filter_query,
            {
                "page": ["3"],
                "per_page": ["10"],
                "query": ["priority:3 AND status:2"],
            },
        )

    def test_ticket_sub_resource_routes(self) -> None:
        transport = RecordingTransport()
        client = make_client(transport)

        client.ticket_conversations("a/b", page=2, per_page=100)
        client.ticket_tasks(42)
        client.ticket_approvals(42)
        client.ticket_requested_items(42)

        self.assertEqual(
            [request["url"] for request in transport.requests],
            [
                "https://example.test/api/v2/tickets/a%2Fb/conversations?page=2&per_page=100",
                "https://example.test/api/v2/tickets/42/tasks?page=1&per_page=30",
                "https://example.test/api/v2/tickets/42/approvals",
                "https://example.test/api/v2/tickets/42/requested_items",
            ],
        )
        self.assertTrue(all(r["method"] == "GET" for r in transport.requests))
        with self.assertRaisesRegex(ValueError, "per_page"):
            client.ticket_conversations(42, per_page=101)
        with self.assertRaisesRegex(ValueError, "resource id"):
            client.ticket_approvals(" ")

    def test_directory_and_knowledge_base_routes(self) -> None:
        transport = RecordingTransport()
        client = make_client(transport)

        client.list_groups(page=2, per_page=50)
        client.get_group(1)
        client.list_requester_groups()
        client.list_workspaces()
        client.get_workspace(2)
        client.list_roles()
        client.get_role(3)
        client.list_announcements(state="archived")
        client.get_announcement(4)
        client.list_canned_responses()
        client.get_canned_response(5)
        client.list_canned_response_folders()
        client.list_solution_categories(workspace_id=2)
        client.get_solution_category(6)
        client.list_solution_folders(6)
        client.get_solution_folder(7)
        client.list_solution_articles("7", per_page=100)
        client.get_solution_article("a/b")
        client.search_solution_articles("vpn access")

        base = "https://example.test/api/v2"
        page = "page=1&per_page=30"
        self.assertEqual(
            [request["url"] for request in transport.requests],
            [
                f"{base}/groups?page=2&per_page=50",
                f"{base}/groups/1",
                f"{base}/requester_groups?{page}",
                f"{base}/workspaces?{page}",
                f"{base}/workspaces/2",
                f"{base}/roles?{page}",
                f"{base}/roles/3",
                f"{base}/announcements?{page}&state=archived",
                f"{base}/announcements/4",
                f"{base}/canned_responses?{page}",
                f"{base}/canned_responses/5",
                f"{base}/canned_response_folders",
                f"{base}/solutions/categories?{page}&workspace_id=2",
                f"{base}/solutions/categories/6",
                f"{base}/solutions/folders?{page}&category_id=6",
                f"{base}/solutions/folders/7",
                f"{base}/solutions/articles?page=1&per_page=100&folder_id=7",
                f"{base}/solutions/articles/a%2Fb",
                f"{base}/solutions/articles/search?{page}&search_term=vpn+access",
            ],
        )
        self.assertTrue(all(r["method"] == "GET" for r in transport.requests))

    def test_directory_and_knowledge_base_arguments_are_validated(self) -> None:
        client = make_client(RecordingTransport())
        with self.assertRaisesRegex(ValueError, "announcement state"):
            client.list_announcements(state="draft")
        with self.assertRaisesRegex(ValueError, "category id"):
            client.list_solution_folders(" ")
        with self.assertRaisesRegex(ValueError, "folder id"):
            client.list_solution_articles("")
        with self.assertRaisesRegex(ValueError, "search term"):
            client.search_solution_articles("  ")
        with self.assertRaisesRegex(ValueError, "per_page"):
            client.list_groups(per_page=0)

    def test_ticket_fields_route(self) -> None:
        transport = RecordingTransport()
        client = make_client(transport)

        client.ticket_fields()
        client.ticket_fields(workspace_id=2)

        self.assertEqual(
            [request["url"] for request in transport.requests],
            [
                "https://example.test/api/v2/ticket_form_fields",
                "https://example.test/api/v2/ticket_form_fields?workspace_id=2",
            ],
        )

    def test_requester_agent_and_department_routes(self) -> None:
        transport = RecordingTransport()
        client = make_client(transport)

        client.list_requesters(
            page=2,
            per_page=20,
            email="person@example.com",
            mobile_phone_number="123",
            work_phone_number="456",
            include_agents=True,
        )
        client.get_requester("7")
        client.list_agents(page=3, per_page=10, query="name:'Ada'", workspace_id=2)
        client.get_agent(8)
        client.get_current_agent()
        client.list_departments(page=4, per_page=5, query="Acme", workspace_id=2)
        client.get_department(9)

        self.assertEqual(
            [urlsplit(request["url"]).path for request in transport.requests],
            [
                "/api/v2/requesters",
                "/api/v2/requesters/7",
                "/api/v2/agents",
                "/api/v2/agents/8",
                "/api/v2/agents/me",
                "/api/v2/departments",
                "/api/v2/departments/9",
            ],
        )
        self.assertIn(
            "include_agents=true",
            urlsplit(transport.requests[0]["url"]).query,
        )

    def test_invalid_arguments_are_rejected(self) -> None:
        client = make_client(RecordingTransport())
        with self.assertRaisesRegex(ValueError, "page"):
            client.list_tickets(page=0)
        with self.assertRaisesRegex(ValueError, "per_page"):
            client.list_requesters(per_page=101)
        with self.assertRaisesRegex(ValueError, "query"):
            client.filter_tickets(" ")

    def test_client_and_request_arguments_are_validated(self) -> None:
        transport = RecordingTransport()
        cases = [
            (
                "api_key",
                lambda: FreshserviceClient(
                    "", base_url="https://example.test", transport=transport
                ),
            ),
            (
                "timeout",
                lambda: FreshserviceClient(
                    "k",
                    base_url="https://example.test",
                    timeout=-1,
                    transport=transport,
                ),
            ),
            (
                "base_url",
                lambda: FreshserviceClient("k", base_url="api/v2", transport=transport),
            ),
            ("'/'", lambda: make_client(transport)._request("GET", "tickets")),
            ("resource id", lambda: make_client(transport).get_agent(" ")),
        ]
        for message, case in cases:
            with (
                self.subTest(message=message),
                self.assertRaisesRegex(ValueError, message),
            ):
                case()
        self.assertEqual(transport.requests, [])

    def test_user_agent_carries_the_package_version(self) -> None:
        transport = RecordingTransport()
        make_client(transport)._request("GET", "/x")
        self.assertEqual(
            transport.requests[0]["headers"]["User-Agent"],
            f"freshservice-cli/{freshservice_cli.__version__}",
        )

    def test_empty_response_body_is_none(self) -> None:
        self.assertIsNone(make_client(RecordingTransport(body=b"")).get_ticket(1))

    def test_api_error_message_fallbacks_never_dump_the_body(self) -> None:
        for body, expected in [
            (b'{"errors": {"message": "bad filter"}}', "bad filter"),
            (b'{"description": "Validation failed"}', "Validation failed"),
            (b'{"message": "rate limited"}', "rate limited"),
            (b'{"errors": [{"field": "query"}]}', "request failed"),
            (b'{"errors": []}', "request failed"),
            (b'["unexpected"]', "request failed"),
            (b"<html>secret-ish page</html>", "request failed"),
        ]:
            transport = RecordingTransport(status_code=400, body=body)
            with self.subTest(body=body), self.assertRaises(ApiError) as raised:
                make_client(transport).get_ticket(1)
            self.assertIn(expected, str(raised.exception))
            self.assertNotIn("secret-ish", str(raised.exception))

    def test_api_error_and_invalid_json_are_safe(self) -> None:
        transport = RecordingTransport(
            status_code=401,
            body=b'{"errors": [{"message": "invalid credentials"}]}',
        )
        with self.assertRaises(ApiError) as raised:
            make_client(transport).get_ticket(1)
        self.assertEqual(raised.exception.status_code, 401)
        self.assertIn("invalid credentials", str(raised.exception))
        self.assertNotIn("test-api-key", str(raised.exception))

        with self.assertRaises(ResponseDecodeError):
            make_client(RecordingTransport(body=b"not-json")).get_ticket(1)

    def test_non_get_requests_are_disabled_before_transport(self) -> None:
        transport = RecordingTransport()
        with self.assertRaises(WriteDisabledError):
            make_client(transport)._request(
                "POST", "/tickets", body={"subject": "test"}
            )
        self.assertEqual(transport.requests, [])

    def test_opted_in_write_encodes_a_json_body(self) -> None:
        transport = RecordingTransport()
        make_client(transport, allow_writes=True)._request(
            "POST", "/tickets", body={"subject": "Café"}
        )
        request = transport.requests[0]
        self.assertEqual(request["method"], "POST")
        self.assertEqual(request["body"], '{"subject": "Café"}'.encode())


class UrllibTransportTests(unittest.TestCase):
    """The default transport, with ``urlopen`` patched so nothing goes online."""

    def test_success_returns_status_body_and_headers(self) -> None:
        response = mock.MagicMock()
        response.getcode.return_value = 200
        response.read.return_value = b'{"ok": true}'
        response.headers.items.return_value = [("Content-Type", "application/json")]
        response.__enter__.return_value = response
        with mock.patch(f"{MODULE}.urlopen", return_value=response) as urlopen:
            result = UrllibTransport().request(
                "GET",
                "https://example.test/x",
                headers={"A": "b"},
                body=None,
                timeout=3,
            )
        self.assertEqual(
            result,
            HttpResponse(200, b'{"ok": true}', {"Content-Type": "application/json"}),
        )
        request = urlopen.call_args.args[0]
        self.assertEqual(
            (request.get_method(), request.full_url), ("GET", "https://example.test/x")
        )
        self.assertEqual(urlopen.call_args.kwargs, {"timeout": 3})

    def test_http_error_is_returned_as_a_response(self) -> None:
        headers = Message()
        headers["Content-Type"] = "application/json"
        body = io.BytesIO(b"{}")
        error = HTTPError("https://example.test/x", 404, "Not Found", headers, body)
        with mock.patch(f"{MODULE}.urlopen", side_effect=error):
            result = UrllibTransport().request(
                "GET", "https://example.test/x", headers={}, body=None, timeout=3
            )
        self.assertEqual(
            result, HttpResponse(404, b"{}", {"Content-Type": "application/json"})
        )

        self.assertTrue(body.closed)

    def test_connection_failures_become_network_errors(self) -> None:
        for failure in (URLError("refused"), TimeoutError(), OSError("reset")):
            with (
                self.subTest(failure=failure),
                mock.patch(f"{MODULE}.urlopen", side_effect=failure),
                self.assertRaisesRegex(NetworkError, "could not reach"),
            ):
                UrllibTransport().request(
                    "GET", "https://example.test/x", headers={}, body=None, timeout=3
                )


if __name__ == "__main__":
    unittest.main()
