"""Developer API client against a fake transport, plus record normalisation."""

from __future__ import annotations

import json
import unittest
from pathlib import Path
from typing import List
from urllib.error import URLError

from tradetron_pnl import api

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "deployments.json"
KEY = "ttk_live_secret_key_0001"


def envelope(data, status=200, headers=None):
    return status, headers or {}, json.dumps({"success": True, "data": data}).encode()


class FakeTransport:
    """Scripted responses keyed by URL suffix; records every request."""

    def __init__(self, routes=None, sequence=None):
        self.routes = routes or {}
        self.sequence = list(sequence or [])
        self.requests: List[tuple] = []

    def __call__(self, url, headers, timeout):
        self.requests.append((url, dict(headers), timeout))
        if self.sequence:
            item = self.sequence.pop(0)
            if isinstance(item, Exception):
                raise item
            return item
        for suffix, response in self.routes.items():
            if url.endswith(suffix) or suffix in url:
                return response
        return 404, {}, b"{}"


def client(transport, **kw):
    return api.TradetronClient(KEY, transport=transport, sleep=lambda _s: None, **kw)


class TestClientRequests(unittest.TestCase):
    def test_bearer_header_and_envelope_unwrap(self):
        t = FakeTransport({"/me/deployments": envelope({"deployments": [{"deployment_id": 1}], "last_page": 1})})
        rows = client(t).list_deployments()
        self.assertEqual(rows, [{"deployment_id": 1}])
        url, headers, _ = t.requests[0]
        self.assertEqual(url, "https://tradetron.tech/api/v1/me/deployments")
        self.assertEqual(headers["Authorization"], f"Bearer {KEY}")
        self.assertEqual(headers["Accept"], "application/json")

    def test_endpoint_paths_are_configurable(self):
        t = FakeTransport({
            "/v2/legs/7": envelope({"positions": [], "positions_hidden": False}),
            "/v2/deps": envelope({"deployments": [], "last_page": 1}),
        })
        c = client(t, base_url="https://example.test/api/", deployments_path="/v2/deps",
                   positions_path="/v2/legs/{id}")
        c.list_deployments()
        c.deployment_positions(7)
        self.assertEqual([r[0] for r in t.requests],
                         ["https://example.test/api/v2/deps", "https://example.test/api/v2/legs/7"])

    def test_pagination_follows_last_page(self):
        t = FakeTransport({
            "/me/deployments?page=2": envelope({"deployments": [{"deployment_id": 2}], "last_page": 2}),
            "/me/deployments": envelope({"deployments": [{"deployment_id": 1}], "last_page": 2}),
        })
        rows = client(t).list_deployments()
        self.assertEqual([r["deployment_id"] for r in rows], [1, 2])
        self.assertEqual(len(t.requests), 2)
        self.assertIn("page=2", t.requests[1][0])

    def test_runaway_pagination_is_refused(self):
        t = FakeTransport({"/me/deployments": envelope({"deployments": [], "last_page": 999})})
        with self.assertRaises(api.ApiError) as ctx:
            client(t).list_deployments(max_pages=3)
        self.assertEqual(ctx.exception.code, "TOO_MANY_PAGES")

    def test_bare_list_and_missing_envelope_are_tolerated(self):
        t = FakeTransport({"/me/deployments": (200, {}, json.dumps({"deployments": [{"id": 5}]}).encode())})
        self.assertEqual(client(t).list_deployments(), [{"id": 5}])
        t = FakeTransport({"/me/deployments": envelope([{"id": 6}])})
        self.assertEqual(client(t).list_deployments(), [{"id": 6}])

    def test_positions_endpoint(self):
        t = FakeTransport({"/me/deployments/1001/positions": envelope(
            {"deployment_id": 1001, "positions_hidden": False, "positions": []})})
        data = client(t).deployment_positions("1001")
        self.assertEqual(data["deployment_id"], 1001)
        self.assertTrue(t.requests[0][0].endswith("/me/deployments/1001/positions"))

    def test_notifications_endpoint(self):
        t = FakeTransport({"/me/notifications": envelope({"notifications": [{"id": 1}]})})
        self.assertEqual(client(t).notifications(), {"notifications": [{"id": 1}]})


class TestClientFailures(unittest.TestCase):
    def test_401_is_not_retried_and_never_leaks_the_key(self):
        t = FakeTransport({"/me/deployments": (401, {}, b'{"success":false,"message":"Unauthenticated"}')})
        with self.assertRaises(api.ApiError) as ctx:
            client(t).list_deployments()
        self.assertEqual(ctx.exception.status, 401)
        self.assertNotIn(KEY, str(ctx.exception))
        self.assertIn("TRADETRON_API_KEY", str(ctx.exception))
        self.assertEqual(len(t.requests), 1)

    def test_404_points_at_base_url(self):
        t = FakeTransport({})
        with self.assertRaises(api.ApiError) as ctx:
            client(t).list_deployments()
        self.assertEqual(ctx.exception.code, "HTTP_404")
        self.assertIn("TRADETRON_API_BASE_URL", str(ctx.exception))

    def test_429_honours_retry_after_then_succeeds(self):
        waits = []
        t = FakeTransport(sequence=[
            (429, {"Retry-After": "2"}, b'{"success":false}'),
            envelope({"deployments": [], "last_page": 1}),
        ])
        c = api.TradetronClient(KEY, transport=t, sleep=waits.append)
        self.assertEqual(c.list_deployments(), [])
        self.assertEqual(waits, [2.0])
        self.assertEqual(c.calls, 2)

    def test_5xx_exhausts_retries(self):
        t = FakeTransport(sequence=[(502, {}, b"bad gateway")] * 3)
        with self.assertRaises(api.ApiError) as ctx:
            client(t, retries=2).list_deployments()
        self.assertTrue(ctx.exception.retryable)
        self.assertEqual(len(t.requests), 3)

    def test_network_error_is_wrapped(self):
        t = FakeTransport(sequence=[URLError("dns down"), URLError("dns down")])
        with self.assertRaises(api.ApiError) as ctx:
            client(t, retries=1).list_deployments()
        self.assertEqual(ctx.exception.code, "NETWORK_ERROR")

    def test_bad_json_and_success_false(self):
        t = FakeTransport({"/me/deployments": (200, {}, b"<html>login</html>")})
        with self.assertRaises(api.ApiError) as ctx:
            client(t).list_deployments()
        self.assertEqual(ctx.exception.code, "INVALID_JSON")
        t = FakeTransport({"/me/deployments": (200, {}, b'{"success": false, "message": "key revoked"}')})
        with self.assertRaises(api.ApiError) as ctx:
            client(t).list_deployments()
        self.assertIn("key revoked", str(ctx.exception))

    def test_missing_deployments_array(self):
        t = FakeTransport({"/me/deployments": envelope({"total": 0})})
        with self.assertRaises(api.ApiError) as ctx:
            client(t).list_deployments()
        self.assertEqual(ctx.exception.code, "INVALID_DEPLOYMENTS")

    def test_empty_key_is_a_config_error(self):
        with self.assertRaises(api.ApiError) as ctx:
            api.TradetronClient("  ")
        self.assertEqual(ctx.exception.code, "NOT_CONFIGURED")

    def test_redact(self):
        self.assertEqual(api.redact(f"token {KEY} here", KEY), "token ttk…01 here")


class TestFixtureClient(unittest.TestCase):
    def test_reads_committed_fixture(self):
        c = api.FixtureClient(FIXTURE)
        rows = c.list_deployments()
        self.assertEqual(len(rows), 4)
        self.assertEqual(c.source, "fixture")
        positions = c.deployment_positions(1001)
        self.assertEqual(len(positions["positions"]), 2)
        empty = c.deployment_positions(4242)
        self.assertEqual(empty["positions"], [])
        self.assertEqual(c.notifications(), [])


class TestNormalisation(unittest.TestCase):
    def test_canonical_record(self):
        raw = api.FixtureClient(FIXTURE).list_deployments()[0]
        dep = api.normalise_deployment(raw)
        self.assertEqual(dep.deployment_id, 1001)
        self.assertEqual(dep.name, "Nifty Weekly Straddle")
        self.assertEqual(dep.pnl, 4250.5)
        self.assertTrue(dep.pnl_known)
        self.assertEqual(dep.open_positions, 4)
        self.assertEqual(dep.currency, "INR")
        self.assertFalse(dep.paper)
        self.assertFalse(dep.in_error)
        self.assertFalse(dep.finished)
        self.assertIs(dep.raw is not raw and dep.raw == raw, True)

    def test_aliases_and_string_numbers(self):
        dep = api.normalise_deployment({
            "id": "77", "strategy_name": "X", "type": "Live Offline", "state": "Error-Broker",
            "net_pnl": "1,234.50", "currency": "usd", "open_legs": "2",
        })
        self.assertEqual(dep.deployment_id, 77)
        self.assertEqual(dep.pnl, 1234.5)
        self.assertTrue(dep.paper)
        self.assertTrue(dep.in_error)
        self.assertEqual(dep.currency, "USD")
        self.assertEqual(dep.open_positions, 2)

    def test_missing_pnl_is_zero_but_flagged(self):
        dep = api.normalise_deployment({"deployment_id": 1, "pnl": None})
        self.assertEqual(dep.pnl, 0.0)
        self.assertFalse(dep.pnl_known)

    def test_record_without_id_is_reported_not_fatal(self):
        deps, problems = api.normalise_deployments([{"name": "no id"}, {"deployment_id": 2}])
        self.assertEqual([d.deployment_id for d in deps], [2])
        self.assertEqual(len(problems), 1)

    def test_finished_statuses(self):
        for status in ("Exited", "Paused", "Blocked"):
            self.assertTrue(api.normalise_deployment({"deployment_id": 1, "status": status}).finished)
        self.assertFalse(api.normalise_deployment({"deployment_id": 1, "status": "Live-Tranching"}).finished)

    def test_positions_normalisation_and_summary(self):
        payload = api.FixtureClient(FIXTURE).deployment_positions(1001)
        pset = api.normalise_positions(payload, 1001)
        self.assertEqual(pset.count, 2)
        self.assertIs(pset.positions_hidden, False)
        summary = api.position_summary(pset.positions)
        self.assertEqual(summary["count"], 2)
        self.assertEqual(summary["pnl"], 4250.5)
        self.assertEqual(summary["net_quantity"], -300)
        self.assertEqual(summary["symbols"][0], "NIFTY26O2125800CE")

    def test_positions_with_unknown_shape_degrade_gracefully(self):
        pset = api.normalise_positions({"deployment_id": 5, "positions": [{"foo": 1}], "positions_hidden": "no"}, 5)
        self.assertIsNone(pset.positions_hidden)
        summary = api.position_summary(pset.positions)
        self.assertEqual(summary["count"], 1)
        self.assertIsNone(summary["pnl"])
        self.assertEqual(summary["symbols"], [])
        self.assertEqual(api.normalise_positions({"positions": "nope"}, 9).count, 0)

    def test_to_float_edge_cases(self):
        self.assertIsNone(api.to_float("N/A"))
        self.assertIsNone(api.to_float(""))
        self.assertEqual(api.to_float("-1,00,000.25"), -100000.25)
        self.assertEqual(api.to_float(True), 1.0)
        self.assertEqual(api.to_int("3.6"), 4)


if __name__ == "__main__":
    unittest.main()
