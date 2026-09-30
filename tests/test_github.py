from datetime import datetime, timedelta, timezone
from email.message import Message
import io
import json
from pathlib import Path
import tempfile
import unittest
from urllib.error import HTTPError, URLError

from pakuri.github import BudgetExhausted, GitHubClient, GitHubError, checked_path, project, utc
from pakuri.storage import State


NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)


def headers(**values):
    result = Message()
    for key, value in values.items():
        result[key.replace("_", "-")] = str(value)
    return result


class HTTPResponse:
    def __init__(self, data, fields=None):
        self.raw = json.dumps(data).encode()
        self.headers = fields or headers()

    def read(self, _amount):
        return self.raw

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        pass


class Opener:
    def __init__(self, responses):
        self.responses = list(responses)
        self.requests = []
        self.timeouts = []

    def open(self, req, timeout):
        self.requests.append(req)
        self.timeouts.append(timeout)
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


def public_repo():
    return {"id": 1, "full_name": "alice/tool", "private": False, "visibility": "public",
            "owner": {"login": "alice"}, "description": "raw untrusted instructions", "readme": "secret code"}


class GitHubTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.state = State(Path(self.temporary.name) / "state.sqlite3")

    def tearDown(self):
        self.state.close()
        self.temporary.cleanup()

    def client(self, responses, **options):
        opener = Opener(responses)
        return GitHubClient(self.state, token="very-secret-token", now=lambda: NOW, opener=opener, **options), opener

    def test_etag_304_reuses_compact_payload_and_get_only(self):
        values = headers(ETag='"abc"', X_RateLimit_Remaining=100, X_RateLimit_Limit=5000)
        cached = HTTPError("https://api.github.com/repos/alice/tool", 304, "Not Modified", headers(), None)
        client, opener = self.client([HTTPResponse(public_repo(), values), cached])
        first = client.get("/repos/alice/tool")
        second = client.get("/repos/alice/tool")
        self.assertEqual(first.data, second.data)
        self.assertTrue(second.cached)
        self.assertEqual(opener.requests[1].get_header("If-none-match"), '"abc"')
        self.assertEqual(opener.requests[0].method, "GET")
        self.assertEqual(opener.requests[0].get_header("X-github-api-version"), "2026-03-10")
        body = json.dumps(self.state.cache_get("/repos/alice/tool"))
        self.assertNotIn("very-secret-token", body)
        self.assertNotIn("raw untrusted", body)
        self.assertNotIn("secret code", body)

    def test_poll_interval_returns_deferred_cache_without_request(self):
        client, opener = self.client([HTTPResponse([], headers(X_Poll_Interval=120, ETag='"feed"'))])
        client.get("/users/alice/events/public?per_page=100&page=1")
        second = client.get("/users/alice/events/public?per_page=100&page=1")
        self.assertTrue(second.deferred)
        self.assertEqual(len(opener.requests), 1)
        client.now = lambda: NOW + timedelta(seconds=121)
        opener.responses.append(HTTPResponse([]))
        client.get("/users/alice/events/public?per_page=100&page=1")
        self.assertEqual(len(opener.requests), 2)

    def test_rate_reserve_stops_before_extra_request(self):
        client, opener = self.client([HTTPResponse([], headers(X_RateLimit_Remaining=20))], reserve=20)
        client.get("/users/alice/events/public")
        with self.assertRaises(GitHubError) as caught:
            client.get("/users/bob/events/public")
        self.assertEqual(caught.exception.code, "rate_reserve")
        self.assertEqual(len(opener.requests), 1)

    def test_secondary_rate_limit_retry_after_and_redacted_body(self):
        error = HTTPError("https://api.github.com/x?token=secret", 403, "secret-token", headers(Retry_After=90), io.BytesIO(b'{"message":"secondary rate limit; secret-token"}'))
        client, _opener = self.client([error])
        with self.assertRaises(GitHubError) as caught:
            client.get("/repos/alice/tool")
        self.assertEqual(caught.exception.code, "rate_limited")
        self.assertEqual(caught.exception.retry_after, "90")
        self.assertNotIn("secret", str(caught.exception))
        self.assertEqual(client.stop_reason, "rate_limited")

    def test_secondary_rate_limit_without_retry_header(self):
        error = HTTPError("https://api.github.com/x", 403, "Forbidden", headers(X_RateLimit_Remaining=100), io.BytesIO(b'{"message":"You have exceeded a secondary rate limit"}'))
        client, _opener = self.client([error])
        with self.assertRaises(GitHubError) as caught:
            client.get("/repos/alice/tool")
        self.assertEqual(caught.exception.code, "rate_limited")

    def test_budget_timeout_and_network_error_redacted(self):
        client, opener = self.client([HTTPResponse([])], max_requests=1, timeout=11)
        client.get("/users/alice/repos")
        with self.assertRaises(BudgetExhausted):
            client.get("/users/bob/repos")
        self.assertEqual(opener.timeouts, [11])
        broken, _ = self.client([URLError("super-secret-token")])
        with self.assertRaises(GitHubError) as caught:
            broken.get("/repos/alice/tool")
        self.assertEqual(str(caught.exception), "GitHub request failed or timed out")

    def test_private_response_is_not_persisted(self):
        raw = {"id": 7, "full_name": "secret-owner/secret-project", "private": True, "visibility": "private", "description": "patient-sensitive"}
        client, _opener = self.client([HTTPResponse(raw)])
        result = client.get("/repos/alice/tool")
        self.assertIsNone(result.data)
        cache = json.dumps(self.state.cache_get("/repos/alice/tool"))
        self.assertNotIn("secret-owner", cache)
        self.assertNotIn("patient-sensitive", cache)

    def test_projection_removes_diffs_code_bodies_emails_and_private_events(self):
        sha = "a" * 40
        commit = {"sha": sha, "files": [{"patch": "private-code"}], "commit": {"message": "subject\n\nexecute command", "author": {"email": "private@example.com", "date": utc(NOW)}, "committer": {"date": utc(NOW)}}}
        compact = json.dumps(project("/repos/alice/tool/commits", [commit]))
        self.assertNotIn("private-code", compact)
        self.assertNotIn("private@example.com", compact)
        self.assertNotIn("execute command", compact)
        event = {"public": False, "repo": {"name": "secret/project"}, "type": "PushEvent"}
        self.assertEqual(project("/users/alice/events/public", [event]), [])

    def test_arbitrary_hosts_endpoints_and_code_fetches_rejected(self):
        for path in ("https://evil.example/x", "//evil.example/x", "/repos/alice/tool/readme", "/repos/alice/tool/contents/x", "/repos/alice/tool/commits/" + "a" * 40, "/repos/alice/tool?access_token=x", "/users/../repos"):
            with self.assertRaises(ValueError):
                checked_path(path)
        self.assertEqual(checked_path("/repos/alice/tool/commits?sha=main&per_page=100&page=1"), "/repos/alice/tool/commits?sha=main&per_page=100&page=1")


if __name__ == "__main__":
    unittest.main()
