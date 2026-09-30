from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import tempfile
import unittest
from urllib.parse import parse_qs, urlsplit

from pakuri.collector import collect, validate_config
from pakuri.github import BudgetExhausted, GitHubError, Response, utc
from pakuri.storage import State, WriterBusy, atomic_write_json, writer_lock

BASE = datetime(2026, 1, 1, 12, tzinfo=timezone.utc)
SHA1 = "a" * 40
SHA2 = "b" * 40


def config(people=None, repositories=None, **options):
    return {"schema_version": 1, "people": people or [], "organizations": [], "repositories": repositories or [],
            "collection": {"push_heads_per_source": 0, "repo_limit_per_owner": 0, **options}}


def person(login="alice"):
    return {"login": login, "topics": ["biology"], "relevance": ["Yuan"]}


def repository(name="alice/tool", identity=123, created=None):
    return {"id": identity, "full_name": name, "private": False, "visibility": "public", "default_branch": "main",
            "owner": {"login": name.split("/")[0]}, "created_at": created or utc(BASE - timedelta(days=10)),
            "updated_at": utc(BASE), "fork": False}


def push(identity="1", when=None, repo="alice/tool", sha=SHA1):
    return {"id": identity, "public": True, "type": "PushEvent", "created_at": when or utc(BASE - timedelta(hours=1)),
            "actor": {"login": "alice"}, "repo": {"id": 123, "name": repo},
            "payload": {"head": sha, "before": "0" * 40, "ref": "refs/heads/main"}}


def commit(sha=SHA1, when=None, title="Implement useful tool"):
    return {"sha": sha, "commit": {"message": title, "committer": {"date": when or utc(BASE - timedelta(hours=1))}, "author": {"date": when or utc(BASE - timedelta(hours=1))}}, "author": {"login": "alice"}}


def release(identity=5, when=None):
    return {"id": identity, "name": "v1.0", "tag_name": "v1.0", "draft": False,
            "published_at": when or utc(BASE - timedelta(hours=1)), "author": {"login": "alice"}}


class FakeClient:
    def __init__(self, routes=None, max_requests=1000):
        self.routes = routes or {}
        self.max_requests = max_requests
        self.requests = self.cache_hits = 0
        self.rate = {}
        self.stop_reason = None
        self.paths = []

    def get(self, path):
        if self.requests >= self.max_requests:
            raise BudgetExhausted()
        self.requests += 1
        self.paths.append(path)
        parsed = urlsplit(path)
        page = int(parse_qs(parsed.query).get("page", ["1"])[0])
        key = (parsed.path, page)
        value = self.routes.get(key, self.routes.get(parsed.path, []))
        if callable(value):
            value = value(path)
        if isinstance(value, Exception):
            raise value
        return value if isinstance(value, Response) else Response(value, received_count=len(value) if isinstance(value, list) else None)


class CollectorTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.state_path = Path(self.temporary.name) / "state.sqlite3"

    def tearDown(self):
        self.temporary.cleanup()

    def run_collect(self, targets, routes=None, now=BASE, **kwargs):
        return collect(targets, self.state_path, client=FakeClient(routes), now=now, **kwargs)

    def test_initial_baseline_and_idempotence(self):
        targets = config(people=[person()])
        routes = {"/users/alice/events/public": [push()]}
        first = self.run_collect(targets, routes)
        self.assertTrue(first["items"][0]["baseline"])
        second = self.run_collect(targets, routes, now=BASE + timedelta(hours=2))
        self.assertEqual(len(second["items"]), 1)
        self.assertEqual(second["items"][0]["observed_at"], utc(BASE))
        self.assertEqual(second["coverage"]["actionable_items"], 0)

    def test_new_event_after_baseline_is_actionable(self):
        targets = config(people=[person()])
        self.run_collect(targets)
        result = self.run_collect(targets, {"/users/alice/events/public": [push("2", utc(BASE + timedelta(minutes=5)))]}, now=BASE + timedelta(hours=1))
        self.assertFalse(result["items"][0]["baseline"])

    def test_late_initial_history_baseline_and_post_initial_late_arrival_kept(self):
        targets = config(people=[person()])
        self.run_collect(targets)
        routes = {"/users/alice/events/public": [push("2", utc(BASE + timedelta(days=1))), push("1", utc(BASE - timedelta(days=1)))]}
        result = self.run_collect(targets, routes, now=BASE + timedelta(days=3))
        self.assertEqual(len(result["items"]), 2)
        values = {item["id"].split(":")[-1]: item for item in result["items"]}
        self.assertTrue(values["1"]["baseline"])
        self.assertFalse(values["2"]["baseline"])
        self.assertEqual(result["coverage"]["late_observed_items"], 2)

    def test_event_retention_gap_is_explicit(self):
        targets = config(people=[person()])
        self.run_collect(targets)
        result = self.run_collect(targets, now=BASE + timedelta(days=31))
        source = next(source for source in result["coverage"]["sources"] if source["source"] == "user_events:alice")
        self.assertEqual(source["gap_hours"], 744)
        self.assertEqual(source["retention_gap_hours"], 24)
        self.assertEqual(result["status"], "partial")

    def test_releases_cover_checkpoint_gap_beyond_requested_window(self):
        targets = config(repositories=[{"full_name": "alice/tool"}])
        self.run_collect(targets, {"/repos/alice/tool": repository()})
        routes = {"/repos/alice/tool": repository(),
                  ("/repos/alice/tool/releases", 1): Response([release(5, utc(BASE + timedelta(days=1)))], link='<https://api.github.com/x>; rel="next"', received_count=100),
                  ("/repos/alice/tool/releases", 2): [release(6, utc(BASE + timedelta(hours=12)))]}
        result = self.run_collect(targets, routes, now=BASE + timedelta(days=3))
        self.assertEqual(len(result["items"]), 2)
        self.assertTrue(all(not item["baseline"] for item in result["items"]))
        self.assertEqual(result["coverage"]["late_observed_items"], 2)

    def test_new_source_added_later_gets_own_baseline(self):
        self.run_collect(config(people=[person()]))
        result = self.run_collect(config(people=[person(), person("bob")]), {"/users/bob/events/public": [push("2", utc(BASE + timedelta(minutes=10)))]}, now=BASE + timedelta(hours=1))
        self.assertTrue(result["items"][0]["baseline"])

    def test_cross_source_commit_identity_and_baseline_immutable(self):
        targets = config(people=[person()], repositories=[{"full_name": "alice/tool", "topics": ["agents"], "relevance": []}], push_heads_per_source=1)
        routes = {"/users/alice/events/public": [push()], "/repos/alice/tool": repository(), "/repos/alice/tool/commits": [commit()]}
        result = self.run_collect(targets, routes)
        commits = [item for item in result["items"] if item["kind"] == "commit"]
        self.assertEqual(len(commits), 1)
        self.assertEqual(commits[0]["topics"], ["agents", "biology"])
        self.assertTrue(commits[0]["baseline"])

    def test_head_only_push_uses_metadata_list_and_matched_sha(self):
        target = config(people=[person()], push_heads_per_source=1)
        fake = FakeClient({"/users/alice/events/public": [push()], "/repos/alice/tool": repository(), "/repos/alice/tool/commits": [commit()]})
        result = collect(target, self.state_path, client=fake, now=BASE)
        self.assertEqual({item["kind"] for item in result["items"]}, {"push", "commit"})
        self.assertIn("/repos/alice/tool/commits?sha=" + SHA1 + "&per_page=1&page=1", fake.paths)
        self.assertFalse(any("/commits/" in path for path in fake.paths))

    def test_partial_failure_keeps_checkpoint_and_partial_baseline_time(self):
        target = config(people=[person()])
        page1 = Response([push()], link='<https://api.github.com/users/alice/events/public?page=2>; rel="next"', received_count=100)
        routes = {("/users/alice/events/public", 1): page1, ("/users/alice/events/public", 2): GitHubError("network_error", "GitHub request failed")}
        first = self.run_collect(target, routes)
        self.assertEqual(first["status"], "partial")
        state = State(self.state_path)
        checkpoint = state.source("user_events:alice")
        state.close()
        self.assertEqual(checkpoint["initialized_at"], utc(BASE))
        self.assertIsNone(checkpoint["last_success"])
        second = self.run_collect(target, {"/users/alice/events/public": [push("2", utc(BASE + timedelta(minutes=15)))]}, now=BASE + timedelta(hours=1))
        new = next(item for item in second["items"] if item["id"].endswith(":2"))
        self.assertFalse(new["baseline"])

    def test_pagination_cap_explicit_and_checkpoint_not_advanced(self):
        target = config(people=[person()], max_pages=1)
        result = self.run_collect(target, {"/users/alice/events/public": Response([push()], link='<https://api.github.com/x>; rel="next"', received_count=100)})
        feed = next(source for source in result["coverage"]["sources"] if source["source"] == "user_events:alice")
        self.assertEqual(feed["status"], "partial")
        self.assertNotIn("checkpoint_after", feed)

    def test_large_owner_cap_is_completed_bounded_scope(self):
        target = config(people=[person()], max_pages=1, repo_limit_per_owner=1)
        routes = {"/users/alice/repos": Response([repository()], link='<https://api.github.com/x>; rel="next"', received_count=100), "/repos/alice/tool": repository()}
        result = self.run_collect(target, routes)
        source = next(source for source in result["coverage"]["sources"] if source["source"] == "user_repositories:alice")
        self.assertEqual(source["status"], "ok")
        self.assertTrue(source["scope_limited"])
        self.assertIn("checkpoint_after", source)

    def test_old_owned_repo_transfer_not_new_creation(self):
        target = config(people=[person()])
        self.run_collect(target)
        rows = [repository(identity=123), repository(name="alice/new", identity=124, created=utc(BASE + timedelta(minutes=5)))]
        result = self.run_collect(target, {"/users/alice/repos": rows}, now=BASE + timedelta(hours=1))
        values = {item["repo"]: item for item in result["items"]}
        self.assertEqual(values["alice/tool"]["kind"], "repository_discovered")
        self.assertTrue(values["alice/tool"]["baseline"])
        self.assertEqual(values["alice/new"]["kind"], "new_repo")
        self.assertFalse(values["alice/new"]["baseline"])

    def test_new_repo_remains_new_repo_across_later_enumerations(self):
        target = config(people=[person()])
        self.run_collect(target)
        row = repository(name="alice/new", identity=124, created=utc(BASE + timedelta(minutes=5)))
        routes = {"/users/alice/repos": [row]}
        first = self.run_collect(target, routes, now=BASE + timedelta(hours=1))
        second = self.run_collect(target, routes, now=BASE + timedelta(hours=2))
        self.assertEqual(first["items"][0]["kind"], "new_repo")
        self.assertEqual(second["items"][0]["kind"], "new_repo")
        self.assertFalse(second["items"][0]["baseline"])

    def test_repo_created_after_initial_but_before_last_scan_kept_new(self):
        target = config(people=[person()])
        self.run_collect(target)
        self.run_collect(target, now=BASE + timedelta(hours=2))
        row = repository(name="alice/delayed", identity=124, created=utc(BASE + timedelta(minutes=5)))
        result = self.run_collect(target, {"/users/alice/repos": [row]}, now=BASE + timedelta(hours=3))
        self.assertEqual(result["items"][0]["kind"], "new_repo")
        self.assertFalse(result["items"][0]["baseline"])

    def test_private_events_and_repository_are_not_collected(self):
        private_push = dict(push(), public=False)
        private_repo = dict(repository(), private=True, visibility="private")
        result = self.run_collect(config(people=[person()], repositories=[{"full_name": "alice/tool"}]), {"/users/alice/events/public": [private_push], "/repos/alice/tool": private_repo})
        self.assertEqual(result["items"], [])
        self.assertTrue(any(source["status"] == "unavailable" for source in result["coverage"]["sources"]))

    def test_future_date_and_naive_clock_rejected(self):
        result = self.run_collect(config(people=[person()]), {"/users/alice/events/public": [push(when=utc(BASE + timedelta(days=1)))]})
        self.assertEqual(result["items"], [])
        self.assertEqual(result["coverage"]["invalid_records_skipped"], 1)
        with self.assertRaises(ValueError):
            self.run_collect(config(people=[person()]), now=BASE.replace(tzinfo=None))

    def test_backward_clock_preserves_checkpoint(self):
        self.run_collect(config(people=[person()]))
        result = self.run_collect(config(people=[person()]), now=BASE - timedelta(hours=1))
        self.assertEqual(result["errors"][0]["code"], "clock_regression")
        self.assertEqual(result["coverage"]["requests"], 0)

    def test_request_budget_rotates_next_unattempted_source(self):
        target = config(people=[person(), person("bob")], max_requests=1)
        first_fake = FakeClient(max_requests=1)
        first = collect(target, self.state_path, client=first_fake, now=BASE)
        second_fake = FakeClient(max_requests=1)
        second = collect(target, self.state_path, client=second_fake, now=BASE + timedelta(hours=1))
        self.assertNotEqual(first_fake.paths[0], second_fake.paths[0])
        self.assertEqual(first["status"], "partial")
        self.assertEqual(second["coverage"]["requests"], 1)

    def test_clipped_output_prioritizes_nonbaseline_and_preserves_archive(self):
        target = config(people=[person()], max_items=1)
        self.run_collect(target, {"/users/alice/events/public": [push()]})
        result = self.run_collect(target, {"/users/alice/events/public": [push("2", utc(BASE + timedelta(minutes=10)))]}, now=BASE + timedelta(hours=1))
        self.assertFalse(result["items"][0]["baseline"])
        self.assertEqual(result["coverage"]["total_window_items"], 2)
        self.assertEqual(result["coverage"]["omitted_item_count"], 1)
        self.assertEqual(result["coverage"]["stored_items"], 2)

    def test_atomic_output_and_writer_exclusion(self):
        output = Path(self.temporary.name) / "latest.json"
        atomic_write_json(output, {"generation": 1})
        atomic_write_json(output, {"generation": 2})
        self.assertEqual(json.loads(output.read_text()), {"generation": 2})
        self.assertEqual(list(output.parent.glob(".pakuri-*.json")), [])
        with writer_lock(self.state_path):
            with self.assertRaises(WriterBusy):
                with writer_lock(self.state_path):
                    pass

    def test_collect_publishes_under_single_writer_lock(self):
        output = Path(self.temporary.name) / "latest.json"
        from unittest.mock import patch
        original = atomic_write_json
        def guarded(path, value):
            with self.assertRaises(WriterBusy):
                with writer_lock(self.state_path):
                    pass
            original(path, value)
        with patch("pakuri.collector.atomic_write_json", side_effect=guarded):
            result = collect(config(people=[person()]), self.state_path, client=FakeClient(), now=BASE, output_path=output)
        self.assertEqual(json.loads(output.read_text()), result)

    def test_expired_cache_pruned_but_observation_archive_preserved(self):
        self.run_collect(config(people=[person()]), {"/users/alice/events/public": [push()]})
        state = State(self.state_path)
        state.cache_put("/repos/alice/tool/commits?since=old", [], "old", "", utc(BASE - timedelta(days=20)), None)
        state.close()
        result = self.run_collect(config(people=[person()]))
        self.assertEqual(result["coverage"]["cache_entries_pruned"], 1)
        self.assertEqual(result["coverage"]["stored_items"], 1)

    def test_config_rejects_urls_duplicate_accounts_and_textual_limits(self):
        for target in (config(people=[person("https://evil.example")]),
                       {"schema_version": 1, "people": [person()], "organizations": [person()]},
                       config(people=[person()], max_requests="unlimited")):
            with self.assertRaises(ValueError):
                validate_config(target)


if __name__ == "__main__":
    unittest.main()
