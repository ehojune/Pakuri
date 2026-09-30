"""Bounded public activity collection with per-source baselines and provenance."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
from urllib.parse import urlencode

from .github import (BudgetExhausted, GitHubClient, GitHubError, SHA, project_event,
                     project_commit, project_release, public_repo, safe_title,
                     timestamp, utc, valid_login, valid_repo)
from .storage import State, atomic_write_json, writer_lock


DEFAULTS = {"max_pages": 3, "repo_limit_per_owner": 5, "max_requests": 300,
            "rate_limit_reserve": 20, "request_timeout_seconds": 15,
            "push_heads_per_source": 2, "max_items": 1000, "cache_retention_days": 7}


def validate_config(config):
    if not isinstance(config, dict) or config.get("schema_version") != 1:
        raise ValueError("targets config requires schema_version=1")
    cleaned = dict(config)
    accounts = set()
    for group, key, validator in (("people", "login", valid_login), ("organizations", "login", valid_login), ("repositories", "full_name", valid_repo)):
        rows = config.get(group, [])
        if not isinstance(rows, list) or len(rows) > 500:
            raise ValueError(group + " must be a list with at most 500 entries")
        seen = set()
        cleaned[group] = []
        for row in rows:
            if not isinstance(row, dict) or not validator(row.get(key)):
                raise ValueError("invalid identifier in " + group)
            identity = row[key].lower()
            if identity in seen or (group != "repositories" and identity in accounts):
                raise ValueError("duplicate target in " + group)
            seen.add(identity)
            if group != "repositories":
                accounts.add(identity)
            item = dict(row)
            for field in ("topics", "relevance"):
                values = row.get(field, [])
                if not isinstance(values, list) or len(values) > 30 or any(not isinstance(value, str) or len(value) > 100 for value in values):
                    raise ValueError(field + " must contain short strings")
                item[field] = sorted(set(values))
            cleaned[group].append(item)
    if not accounts and not cleaned["repositories"]:
        raise ValueError("targets config has no targets")
    options = config.get("collection", {})
    if not isinstance(options, dict):
        raise ValueError("collection must be an object")
    cleaned["collection"] = dict(DEFAULTS, **options)
    bounds = {"max_pages": (1, 20), "repo_limit_per_owner": (0, 100), "max_requests": (1, 5000),
              "rate_limit_reserve": (0, 1000), "request_timeout_seconds": (1, 60), "push_heads_per_source": (0, 10)}
    bounds["max_items"] = (1, 2000)
    bounds["cache_retention_days"] = (1, 365)
    for key, (lower, upper) in bounds.items():
        value = cleaned["collection"][key]
        if isinstance(value, bool) or not isinstance(value, int) or not lower <= value <= upper:
            raise ValueError("invalid collection setting: " + key)
    return cleaned


def load_config(path):
    with open(path, encoding="utf-8-sig") as stream:
        return validate_config(json.load(stream))


class Collector:
    def __init__(self, config, state, client, now, hours):
        self.config, self.state, self.client, self.now, self.hours = config, state, client, now, hours
        self.moment = utc(now)
        self.cutoff = utc(now - timedelta(hours=hours))
        self.options = config["collection"]
        self.errors = []
        self.sources = []
        self.verified = {}
        self.bad_records = 0
        self.heads_unavailable = 0
        self.discovery_skipped = 0

    def error(self, source, exc):
        entry = {"source": source, "code": exc.code, "message": str(exc)}
        if exc.retry_after is not None:
            entry["retry_after"] = exc.retry_after
        self.errors.append(entry)

    def item(self, identity, kind, title, url, published, actor, repo, baseline, target, **extra):
        when = timestamp(published)
        if when is None or when > self.now + timedelta(minutes=5):
            self.bad_records += 1
            return None
        return {"id": identity, "kind": kind, "title": safe_title(title), "url": url,
                "published_at": utc(when), "observed_at": self.moment,
                "actor": actor if valid_login(actor) else "", "repo": repo,
                "baseline": bool(baseline), "topics": target.get("topics", []),
                "relevance": target.get("relevance", []), **extra}

    def save(self, values):
        self.state.save_items([value for value in values if value], self.moment)

    def commit_item(self, row, repo, baseline, target):
        row = project_commit(row)
        if row is None:
            self.bad_records += 1
            return None
        sha = row["sha"].lower()
        commit = row["commit"]
        date = commit.get("committer", {}).get("date") or commit.get("author", {}).get("date")
        actor = row.get("author", {}).get("login") or row.get("committer", {}).get("login") or ""
        return self.item("commit:" + repo.lower() + ":" + sha, "commit", commit["message"],
                         "https://github.com/" + repo + "/commit/" + sha,
                         date, actor, repo, baseline, target, sha=sha, timestamp_basis="commit_committer_date")

    def source_baseline(self, initial, info, date):
        publication = timestamp(date)
        initialized = timestamp(info["initialized_at"]) if info else None
        return initial or (publication is not None and initialized is not None and publication <= initialized)

    def release_item(self, row, repo, baseline, target):
        row = project_release(row)
        if row is None or not str(row.get("id", "")).isdigit():
            self.bad_records += 1
            return None
        from urllib.parse import quote
        return self.item("release:" + repo.lower() + ":" + str(row["id"]), "release", row["name"],
                         "https://github.com/" + repo + "/releases/tag/" + quote(row["tag_name"], safe=""),
                         row["published_at"], row["author"].get("login"), repo, baseline, target,
                         tag=row["tag_name"], timestamp_basis="release_published_at")

    def event_items(self, row, baseline, target):
        row = project_event(row)
        if row is None:
            return [], None
        repo = row["repo"]["name"]
        actor = row["actor"].get("login")
        payload = row["payload"]
        kind = row["type"]
        result, head = None, None
        if kind == "PushEvent" and str(row.get("id", "")).isdigit():
            sha = payload.get("head", "")
            branch = safe_title(payload.get("ref"), "unknown branch")
            url = "https://github.com/" + repo
            if isinstance(sha, str) and SHA.fullmatch(sha):
                sha = sha.lower()
                url += "/commit/" + sha
                head = (repo, sha)
            result = self.item("push:" + repo.lower() + ":" + str(row["id"]), "push",
                               "Push " + branch + (" · " + sha[:12] if head else ""), url,
                               row["created_at"], actor, repo, baseline, target,
                               ref=branch, head=sha if head else "", timestamp_basis="event_created_at")
        elif kind == "CreateEvent" and payload.get("ref_type") == "repository" and str(row["repo"].get("id", "")).isdigit():
            result = self.item("repository:" + str(row["repo"]["id"]), "new_repo", "New repository: " + repo,
                               "https://github.com/" + repo, row["created_at"], actor, repo, baseline, target,
                               timestamp_basis="repository_create_event")
        elif kind == "ReleaseEvent" and payload.get("release"):
            result = self.release_item(payload["release"], repo, baseline, target)
        return [result] if result else [], head if result else None

    def pages(self, source, endpoint, query, consume, *, event_feed=False, stop_before=None, scope_done=None):
        info = self.state.source(source)
        report = {"source": source, "baseline": info is None, "status": "ok", "pages": 0,
                  "records_received": 0, "checkpoint_before": info["last_success"] if info else None}
        prior = timestamp(info["last_success"] or info["initialized_at"]) if info else None
        report["gap_hours"] = round(max(0, (self.now - prior).total_seconds() / 3600), 2) if prior else None
        if event_feed and prior and self.now - prior > timedelta(days=30):
            report["retention_gap_hours"] = round((self.now - prior - timedelta(days=30)).total_seconds() / 3600, 2)
            report["warnings"] = ["checkpoint predates the 30-day Events API retention; omitted older activity cannot be recovered from this feed"]
        self.sources.append(report)
        limit = min(3, self.options["max_pages"]) if event_feed else self.options["max_pages"]
        complete, deferred = False, False
        try:
            for page in range(1, limit + 1):
                path = endpoint + "?" + urlencode(dict(query, per_page=100, page=page))
                response = self.client.get(path)
                if not isinstance(response.data, list):
                    raise GitHubError("invalid_response", "expected a GitHub metadata list")
                report["pages"] += 1
                deferred |= response.deferred
                count = response.received_count if response.received_count is not None else len(response.data)
                report["records_received"] += count
                consume(response.data, info is None, info)
                if not response.deferred and info is None:
                    # Baseline completion and successful scan checkpoint are separate.
                    # Partial snapshots never repeatedly rebaseline genuinely new data.
                    self.state.initialize(source, self.moment)
                if response.deferred:
                    report["status"] = "deferred"
                    report["reason"] = "X-Poll-Interval not elapsed; cached public metadata reused"
                    break
                has_next = 'rel="next"' in response.link
                if scope_done and scope_done():
                    report["scope_limited"] = True
                    report["reason"] = "owned repository polling cap; bounded selected scope completed"
                    complete = True
                    break
                reached_old = False
                if stop_before and response.data:
                    dates = [timestamp(row.get("published_at") or (row.get("commit") or {}).get("committer", {}).get("date")) for row in response.data]
                    valid = [date for date in dates if date]
                    reached_old = bool(valid) and max(valid) < stop_before
                if not has_next or reached_old:
                    complete = True
                    break
            if not complete and not deferred:
                report["status"] = "partial"
                report["reason"] = "configured page cap reached; checkpoint unchanged"
            if complete and not deferred:
                self.state.checkpoint(source, self.moment)
                report["checkpoint_after"] = self.moment
        except GitHubError as exc:
            report["status"] = "failed"
            report["reason"] = exc.code
            self.error(source, exc)
            if isinstance(exc, BudgetExhausted) or self.client.stop_reason:
                raise
        return report

    def verify_repository(self, name):
        normalized = name.lower()
        if normalized not in self.verified:
            response = self.client.get("/repos/" + name)
            if not public_repo(response.data):
                self.state.repository_remove(name)
                self.verified[normalized] = None
            else:
                self.verified[normalized] = response.data
        return self.verified[normalized]

    def events(self, target, organization=False):
        login = target["login"]
        source = ("org_events:" if organization else "user_events:") + login.lower()
        heads = []
        def consume(rows, baseline, _info):
            for row in rows:
                event_baseline = self.source_baseline(baseline, _info, row.get("created_at"))
                values, head = self.event_items(row, event_baseline, target)
                self.save(values)
                if head and timestamp(row.get("created_at")) and utc(timestamp(row["created_at"])) >= self.cutoff:
                    heads.append((head, event_baseline))
        endpoint = "/orgs/" + login + "/events" if organization else "/users/" + login + "/events/public"
        self.pages(source, endpoint, {}, consume, event_feed=True)
        # PushEvent no longer embeds commits. Resolve only bounded head metadata;
        # intermediate commits and non-default branch history are not guaranteed.
        for (repo, sha), baseline in heads[:self.options["push_heads_per_source"]]:
            try:
                if self.verify_repository(repo):
                    response = self.client.get("/repos/" + repo + "/commits?" + urlencode({"sha": sha, "per_page": 1, "page": 1}))
                    if isinstance(response.data, list) and response.data and response.data[0].get("sha", "").lower() == sha:
                        self.save([self.commit_item(response.data[0], repo, baseline, target)])
                    else:
                        self.heads_unavailable += 1
                else:
                    self.heads_unavailable += 1
            except GitHubError as exc:
                self.heads_unavailable += 1
                self.error(source + ":push_head", exc)
                if isinstance(exc, BudgetExhausted) or self.client.stop_reason:
                    raise

    def repositories(self, target, organization=False):
        login = target["login"]
        source = ("org_repositories:" if organization else "user_repositories:") + login.lower()
        selected = []
        all_count = 0
        def consume(rows, baseline, info):
            nonlocal all_count
            for row in rows:
                if not public_repo(row) or row["full_name"].split("/")[0].lower() != login.lower():
                    continue
                all_count += 1
                repo = row["full_name"]
                # Enumeration discovers public owned repositories, including forks/transfers.
                # An older created_at cannot be presented as a newly created repository.
                created = timestamp(row.get("created_at"))
                previous = timestamp(info["initialized_at"]) if info else None
                newly_created = not baseline and created is not None and previous is not None and created > previous
                value = self.item("repository:" + str(row["id"]), "new_repo" if newly_created else "repository_discovered",
                                  ("New repository: " if newly_created else "Observed repository: ") + repo,
                                  "https://github.com/" + repo, row.get("created_at"), login, repo,
                                  not newly_created, target, fork=bool(row.get("fork")), timestamp_basis="repository_created_at")
                if value and str(row.get("id", "")).isdigit():
                    self.save([value])
                if len(selected) < self.options["repo_limit_per_owner"]:
                    selected.append(repo)
                    self.state.repository_put(row, source)
                    self.verified[repo.lower()] = row
        endpoint = ("/orgs/" if organization else "/users/") + login + "/repos"
        report = self.pages(source, endpoint, {"type": "public" if organization else "owner", "sort": "updated", "direction": "desc"}, consume,
                            scope_done=lambda: len(selected) >= self.options["repo_limit_per_owner"])
        report["repositories_selected"] = len(selected)
        report["repository_poll_cap"] = self.options["repo_limit_per_owner"]
        report["repositories_not_selected"] = max(0, all_count - len(selected))
        self.discovery_skipped += max(0, all_count - len(selected))

    def repo_activity(self, target):
        name = target["full_name"]
        try:
            metadata = self.verify_repository(name)
            if not metadata:
                self.sources.append({"source": "repo:" + name.lower(), "status": "unavailable", "reason": "repository is not confirmed public"})
                return
            self.state.repository_put(metadata, "curated" if target.get("_curated") else target["_origin"])
        except GitHubError as exc:
            self.sources.append({"source": "repo:" + name.lower(), "status": "failed", "reason": exc.code})
            self.error("repo:" + name.lower(), exc)
            if isinstance(exc, BudgetExhausted) or self.client.stop_reason:
                raise
            return
        branch = metadata.get("default_branch")
        if not isinstance(branch, str) or not branch or len(branch) > 200:
            self.sources.append({"source": "commits:" + name.lower(), "status": "unavailable", "reason": "no default branch"})
        else:
            key = "commits:" + name.lower()
            checkpoint = self.state.source(key)
            previous = timestamp(checkpoint["last_success"]) if checkpoint else None
            # Overlap accommodates feed delay and retries; baseline handles existing history.
            since = min(self.now - timedelta(hours=max(self.hours, 6)), previous - timedelta(hours=6)) if previous else self.now - timedelta(hours=max(self.hours, 24))
            self.pages(key, "/repos/" + name + "/commits", {"sha": branch, "since": utc(since)},
                       lambda rows, baseline, info: self.save([self.commit_item(row, name, self.source_baseline(baseline, info, (row.get("commit") or {}).get("committer", {}).get("date")), target) for row in rows]))
        release_checkpoint = self.state.source("releases:" + name.lower())
        previous_release_poll = timestamp(release_checkpoint["last_success"]) if release_checkpoint else None
        release_cutoff = min(self.now - timedelta(hours=self.hours), previous_release_poll - timedelta(hours=6)) if previous_release_poll else self.now - timedelta(hours=self.hours)
        self.pages("releases:" + name.lower(), "/repos/" + name + "/releases", {},
                   lambda rows, baseline, info: self.save([self.release_item(row, name, self.source_baseline(baseline, info, row.get("published_at")), target) for row in rows]),
                   stop_before=release_cutoff)

    def jobs(self):
        jobs = {}
        owners = {}
        for organization, group in ((False, "people"), (True, "organizations")):
            for target in self.config[group]:
                prefix = "org" if organization else "user"
                origin = prefix + "_repositories:" + target["login"].lower()
                owners[origin] = target
                jobs[prefix + "_events:" + target["login"].lower()] = ("events", target, organization)
                jobs[origin] = ("repositories", target, organization)
        grouped = {}
        for repo, origin in self.state.repositories():
            if origin in owners:
                grouped.setdefault(origin, []).append(repo)
        for origin, rows in grouped.items():
            rows.sort(key=lambda row: row.get("updated_at") or "", reverse=True)
            for row in rows[:self.options["repo_limit_per_owner"]]:
                target = dict(owners[origin], full_name=row["full_name"], _origin=origin)
                jobs["repo:" + row["full_name"].lower()] = ("repo_activity", target, None)
        for repo in self.config["repositories"]:
            jobs["repo:" + repo["full_name"].lower()] = ("repo_activity", dict(repo, _curated=True), None)
        return jobs

    def run(self):
        done = set()
        jobs = self.jobs()
        cursor = self.state.setting("scheduler_cursor")
        ordered = sorted(jobs)
        if cursor:
            ordered = [key for key in ordered if key > cursor] + [key for key in ordered if key <= cursor]
        stop = False
        while ordered:
            key = ordered.pop(0)
            method, target, organization = jobs[key]
            before_requests = self.client.requests
            try:
                if method == "repo_activity":
                    self.repo_activity(target)
                else:
                    getattr(self, method)(target, organization)
            except GitHubError:
                stop = True
            done.add(key)
            # Advance only if actual requests were issued. A budget failure then starts
            # with that previously unattempted job on the next run.
            if self.client.requests > before_requests and not stop:
                self.state.set_setting("scheduler_cursor", key)
            if stop:
                break
            expanded = self.jobs()
            for added in sorted(set(expanded) - set(jobs)):
                jobs[added] = expanded[added]
                ordered.append(added)
        for key in sorted(set(jobs) - done):
            self.sources.append({"source": key, "status": "skipped", "reason": "request budget or rate control"})
        all_items = self.state.output_items(self.cutoff)
        # Bound the consumer JSON while leaving every compact observation in SQLite.
        priority = {"release": 0, "new_repo": 1, "push": 2, "commit": 3, "repository_discovered": 4}
        all_items.sort(key=lambda item: (item["baseline"], priority.get(item["kind"], 5), -timestamp(item["published_at"]).timestamp(), item["id"]))
        items = all_items[:self.options["max_items"]]
        complete = sum(source["status"] == "ok" for source in self.sources)
        issues = any(source["status"] in {"failed", "partial", "skipped", "unavailable"} or source.get("retention_gap_hours") is not None for source in self.sources) or bool(self.errors)
        status = "partial" if issues else "ok"
        if issues and complete == 0 and not items:
            status = "error"
        return {"schema_version": 1, "generated_at": self.moment, "window_hours": self.hours,
                "status": status, "items": items, "errors": self.errors,
                "coverage": {"sources": self.sources, "requests": self.client.requests,
                             "cache_hits": self.client.cache_hits, "rate_limit": self.client.rate,
                             "people": len(self.config["people"]), "organizations": len(self.config["organizations"]),
                             "curated_repositories": len(self.config["repositories"]),
                             "baseline_items": sum(item["baseline"] for item in all_items),
                             "actionable_items": sum(not item["baseline"] for item in all_items),
                             "late_observed_items": sum(item["published_at"] < self.cutoff for item in all_items),
                             "stored_items": self.state.count_items(), "output_items": len(items),
                             "total_window_items": len(all_items), "omitted_item_count": len(all_items) - len(items),
                             "max_output_items": self.options["max_items"],
                             "invalid_records_skipped": self.bad_records, "push_heads_unavailable": self.heads_unavailable,
                             "owned_repositories_not_selected": self.discovery_skipped,
                             "output_selection": "published_at OR first observed_at inside requested UTC window; late arrivals retained",
                             "storage_retention": "no automatic item deletion; rolling output only",
                             "limitations": ["Events API: last 30 days, at most 300 events per feed; delivery delay 30 seconds to 6 hours",
                                             "Owned repository polling is capped per owner; selected by latest updated_at",
                                             "Commit list polling covers the default branch; bounded PushEvent head hydration covers observed other branches",
                                             "PushEvent contains head/before only; intermediate commits and complete push history are not guaranteed",
                                             "Page and request caps can leave gaps; incomplete sources keep their prior checkpoints",
                                             "Commit timestamps describe commits; push event timestamps describe pushes",
                                             "First observation per source is baseline and must not be sent as a new-event alert",
                                             "A successful collection is a bounded snapshot, not proof of complete GitHub history"]}}


def collect(config, state_path, *, client=None, now=None, hours=24, max_requests=None, output_path=None):
    config = validate_config(config)
    if isinstance(hours, bool) or not isinstance(hours, int) or not 1 <= hours <= 720:
        raise ValueError("hours must be an integer from 1 to 720")
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        raise ValueError("collection clock must have a timezone")
    now = now.astimezone(timezone.utc)
    with writer_lock(state_path):
        state = State(state_path)
        try:
            previous = timestamp(state.setting("last_run_at"))
            if previous and now + timedelta(minutes=5) < previous:
                result = {"schema_version": 1, "generated_at": utc(now), "window_hours": hours,
                        "status": "error", "items": [], "errors": [{"source": "clock", "code": "clock_regression", "message": "local UTC clock moved backwards; collection not performed"}],
                        "coverage": {"sources": [], "requests": 0, "limitations": ["state checkpoints were preserved"]}}
                if output_path is not None:
                    atomic_write_json(output_path, result)
                return result
            deleted_cache = state.prune_cache(utc(now - timedelta(days=config["collection"]["cache_retention_days"])))
            if client is None:
                options = config["collection"]
                client = GitHubClient(state, max_requests=max_requests or options["max_requests"],
                                      reserve=options["rate_limit_reserve"], timeout=options["request_timeout_seconds"], now=lambda: now)
            elif hasattr(client, "state"):
                client.state = state
            if max_requests is not None:
                if isinstance(max_requests, bool) or not isinstance(max_requests, int) or not 1 <= max_requests <= 5000:
                    raise ValueError("max_requests must be from 1 to 5000")
                client.max_requests = max_requests
            result = Collector(config, state, client, now, hours).run()
            result["coverage"]["cache_retention_days"] = config["collection"]["cache_retention_days"]
            result["coverage"]["cache_entries_pruned"] = deleted_cache
            state.set_setting("last_run_at", utc(now))
            if output_path is not None:
                # Publication stays inside the collection's single-writer lock.
                atomic_write_json(output_path, result)
            return result
        finally:
            state.close()
