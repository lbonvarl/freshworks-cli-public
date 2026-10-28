from __future__ import annotations

import io
import json
import unittest
from email.message import Message
from unittest import mock
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlsplit

import freshsales_cli
from freshsales_cli.client import FreshsalesClient, HttpResponse, UrllibTransport
from freshsales_cli.errors import (
    ApiError,
    NetworkError,
    ResponseDecodeError,
    WriteDisabledError,
)

MODULE = "freshsales_cli.client"


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
) -> FreshsalesClient:
    return FreshsalesClient(
        "test-api-key",
        base_url="https://example.test/crm/sales/api",
        transport=transport,
        timeout=7,
        allow_writes=allow_writes,
    )


class ReadRequestTests(unittest.TestCase):
    def test_detail_request_sets_auth_and_encodes_include(self) -> None:
        transport = RecordingTransport(body=b'{"contact": {"id": 42}}')
        result = make_client(transport).get_contact(
            "a/b", include=["owner", "sales_accounts"]
        )

        self.assertEqual(result, {"contact": {"id": 42}})
        request = transport.requests[0]
        self.assertEqual(request["method"], "GET")
        self.assertEqual(
            request["url"],
            "https://example.test/crm/sales/api/contacts/a%2Fb?include=owner%2Csales_accounts",
        )
        headers = request["headers"]
        self.assertIsInstance(headers, dict)
        self.assertEqual(headers["Authorization"], "Token token=test-api-key")
        self.assertEqual(headers["Accept"], "application/json")
        self.assertIsNone(request["body"])

    def test_list_and_filter_routes(self) -> None:
        transport = RecordingTransport()
        client = make_client(transport)

        client.contact_filters()
        client.list_contacts("4", page=2, sort="updated_at", sort_type="desc")
        client.account_filters()
        client.list_accounts("5")
        client.deal_filters()
        client.list_deals("6", include="sales_account")

        self.assertEqual(
            [request["url"] for request in transport.requests],
            [
                "https://example.test/crm/sales/api/contacts/filters",
                "https://example.test/crm/sales/api/contacts/view/4?page=2&sort=updated_at&sort_type=desc",
                "https://example.test/crm/sales/api/sales_accounts/filters",
                "https://example.test/crm/sales/api/sales_accounts/view/5?page=1",
                "https://example.test/crm/sales/api/deals/filters",
                "https://example.test/crm/sales/api/deals/view/6?page=1&include=sales_account",
            ],
        )

    def test_account_and_deal_detail_routes(self) -> None:
        transport = RecordingTransport()
        client = make_client(transport)

        client.get_account("5", include=["contacts", "deals"])
        client.get_deal(6)

        self.assertEqual(
            [request["url"] for request in transport.requests],
            [
                "https://example.test/crm/sales/api/sales_accounts/5?include=contacts%2Cdeals",
                "https://example.test/crm/sales/api/deals/6",
            ],
        )

    def test_search_and_lookup_query_parameters(self) -> None:
        transport = RecordingTransport()
        client = make_client(transport)

        client.search("Acme & Co", include=["contact", "deal"], per_page=25)
        client.lookup(
            "person@example.com",
            field="email",
            entities="contact,sales_account",
        )

        search_query = parse_qs(urlsplit(transport.requests[0]["url"]).query)
        lookup_query = parse_qs(urlsplit(transport.requests[1]["url"]).query)
        self.assertEqual(
            search_query,
            {"q": ["Acme & Co"], "include": ["contact,deal"], "per_page": ["25"]},
        )
        self.assertEqual(
            lookup_query,
            {
                "q": ["person@example.com"],
                "f": ["email"],
                "entities": ["contact,sales_account"],
            },
        )

    def test_campaign_list_and_activity_routes(self) -> None:
        transport = RecordingTransport()
        client = make_client(transport)

        client.campaigns()
        client.selector("lead_sources")
        client.marketing_lists(page=2)
        client.marketing_list_contacts("9", page=3)
        client.contact_activities(42, include="user", limit=50)
        client.contact_activities(43)

        self.assertEqual(
            [request["url"] for request in transport.requests],
            [
                "https://example.test/crm/sales/api/selector/campaigns",
                "https://example.test/crm/sales/api/selector/lead_sources",
                "https://example.test/crm/sales/api/lists?page=2",
                "https://example.test/crm/sales/api/contacts/lists/9?page=3",
                "https://example.test/crm/sales/api/contacts/42/activities?include=user&limit=50",
                "https://example.test/crm/sales/api/contacts/43/activities",
            ],
        )
        self.assertTrue(all(r["method"] == "GET" for r in transport.requests))

    def test_task_and_appointment_routes(self) -> None:
        transport = RecordingTransport()
        client = make_client(transport)

        client.list_tasks()
        client.list_tasks(filter_name="completed", page=2, include="owner,targetable")
        client.get_task("a/b", include=["users"])
        client.list_appointments()
        client.list_appointments(filter_name="past", page=3)
        client.get_appointment(9)

        base = "https://example.test/crm/sales/api"
        self.assertEqual(
            [request["url"] for request in transport.requests],
            [
                f"{base}/tasks?filter=open&page=1",
                f"{base}/tasks?filter=completed&page=2&include=owner%2Ctargetable",
                f"{base}/tasks/a%2Fb?include=users",
                f"{base}/appointments?filter=upcoming&page=1",
                f"{base}/appointments?filter=past&page=3",
                f"{base}/appointments/9",
            ],
        )
        self.assertTrue(all(r["method"] == "GET" for r in transport.requests))
        with self.assertRaisesRegex(ValueError, "task filter"):
            client.list_tasks(filter_name="all")
        with self.assertRaisesRegex(ValueError, "appointment filter"):
            client.list_appointments(filter_name="today")
        with self.assertRaisesRegex(ValueError, "page"):
            client.list_tasks(page=0)

    def test_field_metadata_routes(self) -> None:
        transport = RecordingTransport()
        client = make_client(transport)

        client.fields("contacts", include=["field_group"])
        client.fields("accounts")
        client.fields("deals")
        client.fields("sales-activities")

        self.assertEqual(
            [request["url"] for request in transport.requests],
            [
                "https://example.test/crm/sales/api/settings/contacts/fields?include=field_group",
                "https://example.test/crm/sales/api/settings/sales_accounts/fields",
                "https://example.test/crm/sales/api/settings/deals/fields",
                "https://example.test/crm/sales/api/settings/sales_activities/fields",
            ],
        )
        self.assertTrue(all(r["method"] == "GET" for r in transport.requests))
        with self.assertRaisesRegex(ValueError, "field entity"):
            client.fields("sales_accounts")

    def test_campaign_argument_validation(self) -> None:
        client = make_client(RecordingTransport())
        with self.assertRaisesRegex(ValueError, "limit"):
            client.contact_activities(1, limit=0)
        with self.assertRaisesRegex(ValueError, "selector"):
            client.selector("  ")
        with self.assertRaisesRegex(ValueError, "page"):
            client.marketing_list_contacts(1, page=0)

    def test_invalid_pagination_and_search_arguments_are_rejected(self) -> None:
        client = make_client(RecordingTransport())
        with self.assertRaisesRegex(ValueError, "page"):
            client.list_contacts(1, page=0)
        with self.assertRaisesRegex(ValueError, "per_page"):
            client.search("term", include="contact", per_page=101)
        with self.assertRaisesRegex(ValueError, "include"):
            client.search("term", include="")

    def test_client_and_request_arguments_are_validated(self) -> None:
        transport = RecordingTransport()
        cases = [
            (
                "api_key",
                lambda: FreshsalesClient(
                    " ", base_url="https://example.test", transport=transport
                ),
            ),
            (
                "timeout",
                lambda: FreshsalesClient(
                    "k", base_url="https://example.test", timeout=0, transport=transport
                ),
            ),
            (
                "base_url",
                lambda: FreshsalesClient("k", base_url="/crm", transport=transport),
            ),
            (
                "base_url",
                lambda: FreshsalesClient(
                    "k", base_url="ftp://example.test", transport=transport
                ),
            ),
            ("'/'", lambda: make_client(transport)._request("GET", "contacts")),
            ("resource id", lambda: make_client(transport).get_contact(" ")),
            (
                "entities",
                lambda: make_client(transport).lookup(
                    "a@b.example", field="email", entities=""
                ),
            ),
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
            f"freshsales-cli/{freshsales_cli.__version__}",
        )

    def test_empty_response_body_is_none(self) -> None:
        self.assertIsNone(make_client(RecordingTransport(body=b"")).get_contact(1))

    def test_api_error_message_fallbacks_never_dump_the_body(self) -> None:
        for body, expected in [
            (b'{"message": "rate limited"}', "rate limited"),
            (b'{"errors": {"code": "500"}}', "request failed"),
            (b'["unexpected"]', "request failed"),
            (b"<html>secret-ish page</html>", "request failed"),
            (b"\xff", "request failed"),
        ]:
            transport = RecordingTransport(status_code=500, body=body)
            with self.subTest(body=body), self.assertRaises(ApiError) as raised:
                make_client(transport).get_contact(1)
            self.assertEqual(
                str(raised.exception), f"Freshsales API error (500): {expected}"
            )

    def test_api_error_extracts_structured_message(self) -> None:
        transport = RecordingTransport(
            status_code=401,
            body=b'{"errors": {"code": "401", "message": "invalid credentials"}}',
        )
        with self.assertRaises(ApiError) as raised:
            make_client(transport).get_contact(1)
        self.assertEqual(raised.exception.status_code, 401)
        self.assertIn("invalid credentials", str(raised.exception))
        self.assertNotIn("test-api-key", str(raised.exception))

    def test_invalid_json_response_is_reported_without_dumping_body(self) -> None:
        transport = RecordingTransport(body=b"not-json")
        with self.assertRaises(ResponseDecodeError):
            make_client(transport).get_contact(1)


class WriteRequestTests(unittest.TestCase):
    def test_entity_builders_reject_non_mapping_data_before_sending(self) -> None:
        transport = RecordingTransport()
        client = make_client(transport, allow_writes=True)
        for case in [
            lambda: client.create_contact(["not", "a", "mapping"]),  # type: ignore[arg-type]
            lambda: client.upsert_account("name", {"name": "x"}),  # type: ignore[arg-type]
            lambda: client.upsert_deal({"name": "x"}, "data"),  # type: ignore[arg-type]
        ]:
            with self.subTest(case=case), self.assertRaises(TypeError):
                case()
        self.assertEqual(transport.requests, [])

    def test_writes_are_disabled_by_default_before_transport_is_called(self) -> None:
        transport = RecordingTransport()
        client = make_client(transport)

        with self.assertRaises(WriteDisabledError):
            client.create_contact({"last_name": "Example"})
        self.assertEqual(transport.requests, [])

    def test_create_and_update_payloads(self) -> None:
        transport = RecordingTransport()
        client = make_client(transport, allow_writes=True)

        client.create_contact({"last_name": "Contact"})
        client.update_contact(10, {"job_title": "Engineer"})
        client.create_account({"name": "Account"})
        client.update_account(11, {"website": "https://example.test"})
        client.create_deal({"name": "Deal", "amount": 100})
        client.update_deal(12, {"probability": 50})

        expected = [
            ("POST", "/contacts", {"contact": {"last_name": "Contact"}}),
            ("PUT", "/contacts/10", {"contact": {"job_title": "Engineer"}}),
            ("POST", "/sales_accounts", {"sales_account": {"name": "Account"}}),
            (
                "PUT",
                "/sales_accounts/11",
                {"sales_account": {"website": "https://example.test"}},
            ),
            ("POST", "/deals", {"deal": {"name": "Deal", "amount": 100}}),
            ("PUT", "/deals/12", {"deal": {"probability": 50}}),
        ]
        self.assertEqual(len(transport.requests), len(expected))
        for request, (method, path, payload) in zip(transport.requests, expected):
            self.assertEqual(request["method"], method)
            self.assertEqual(urlsplit(request["url"]).path, f"/crm/sales/api{path}")
            self.assertEqual(json.loads(request["body"]), payload)

    def test_upsert_payloads(self) -> None:
        transport = RecordingTransport()
        client = make_client(transport, allow_writes=True)

        client.upsert_contact(
            {"emails": "contact@example.com"}, {"last_name": "Contact"}
        )
        client.upsert_account({"name": "Account"}, {"city": "Paris"})
        client.upsert_deal({"name": "Deal"}, {"amount": 100})

        self.assertEqual(
            [
                (
                    urlsplit(request["url"]).path,
                    json.loads(request["body"]),
                )
                for request in transport.requests
            ],
            [
                (
                    "/crm/sales/api/contacts/upsert",
                    {
                        "unique_identifier": {"emails": "contact@example.com"},
                        "contact": {"last_name": "Contact"},
                    },
                ),
                (
                    "/crm/sales/api/sales_accounts/upsert",
                    {
                        "unique_identifier": {"name": "Account"},
                        "sales_account": {"city": "Paris"},
                    },
                ),
                (
                    "/crm/sales/api/deals/upsert",
                    {
                        "unique_identifier": {"name": "Deal"},
                        "deal": {"amount": 100},
                    },
                ),
            ],
        )


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
