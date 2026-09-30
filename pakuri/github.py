"""GET-only GitHub client. Responses are projected before cache persistence."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import json
import os
import re
import subprocess
from urllib import error, parse, request

API_VERSION = "2026-03-10"
LOGIN = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})\Z")
REPOSITORY = re.compile(r"[A-Za-z0-9_.-]{1,100}\Z")
SHA = re.compile(r"[0-9a-fA-F]{40,64}\Z")


def valid_login(value):
    return isinstance(value, str) and LOGIN.fullmatch(value) is not None


def valid_repo(value):
    if not isinstance(value, str) or value.count("/") != 1:
        return False
    owner, name = value.split("/")
    return valid_login(owner) and bool(REPOSITORY.fullmatch(name)) and name not in {".", ".."}


def utc(value: datetime):
    return value.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def timestamp(value):
    if not isinstance(value, str):
        return None
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return result.astimezone(timezone.utc) if result.tzinfo is not None else None
    except (ValueError, OverflowError):
        return None


def safe_title(value, fallback="Untitled"):
    if not isinstance(value, str):
        return fallback
    # External prose remains untrusted data; control characters and long bodies stay out.
    first = value.splitlines()[0] if value.splitlines() else ""
    return "".join(ch for ch in first if ch.isprintable())[:300] or fallback


def public_repo(data):
    return isinstance(data, dict) and data.get("private") is False and data.get("visibility", "public") == "public" and valid_repo(data.get("full_name"))


def project_repository(value):
    if not public_repo(value):
        return None
    result = {key: value.get(key) for key in ("id", "full_name", "private", "visibility", "created_at", "pushed_at", "updated_at", "default_branch", "fork", "archived")}
    result["owner"] = {"login": value.get("owner", {}).get("login")}
    result["html_url"] = "https://github.com/" + value["full_name"]
    return result


def project_commit(value):
    if not isinstance(value, dict) or not SHA.fullmatch(str(value.get("sha", ""))):
        return None
    raw = value.get("commit") or {}
    result = {"sha": value["sha"], "commit": {"message": safe_title(raw.get("message"), "Commit " + value["sha"][:12])}}
    for role in ("author", "committer"):
        result[role] = {"login": (value.get(role) or {}).get("login")}
        result["commit"][role] = {"date": (raw.get(role) or {}).get("date")}
    # Deliberately discard code, patches, trees, files and author email/name.
    return result


def project_release(value):
    if not isinstance(value, dict) or value.get("draft") is not False:
        return None
    return {"id": value.get("id"), "name": safe_title(value.get("name"), safe_title(value.get("tag_name"))),
            "tag_name": safe_title(value.get("tag_name")), "published_at": value.get("published_at"),
            "draft": False, "author": {"login": (value.get("author") or {}).get("login")}}


def project_event(value):
    if not isinstance(value, dict) or value.get("public") is not True or not valid_repo((value.get("repo") or {}).get("name")):
        return None
    if value.get("type") not in {"PushEvent", "CreateEvent", "ReleaseEvent"}:
        return None
    payload = value.get("payload") or {}
    keep = {key: payload.get(key) for key in ("ref", "head", "before", "ref_type", "action", "push_id") if key in payload}
    if "release" in payload:
        keep["release"] = project_release(payload["release"])
    return {"id": value.get("id"), "type": value.get("type"), "public": True,
            "created_at": value.get("created_at"), "actor": {"login": (value.get("actor") or {}).get("login")},
            "repo": {"id": value["repo"].get("id"), "name": value["repo"]["name"]}, "payload": keep}


def project(path, data):
    endpoint = parse.urlsplit(path).path
    if endpoint.endswith("/events/public") or re.fullmatch(r"/orgs/[^/]+/events", endpoint):
        converter = project_event
    elif endpoint.endswith("/releases"):
        converter = project_release
    elif "/commits" in endpoint:
        converter = project_commit
    else:
        converter = project_repository
    if isinstance(data, list):
        return [compact for row in data if (compact := converter(row)) is not None]
    return converter(data)


class GitHubError(RuntimeError):
    def __init__(self, code, message, *, retry_after=None):
        super().__init__(message)
        self.code, self.retry_after = code, retry_after


class BudgetExhausted(GitHubError):
    def __init__(self):
        super().__init__("budget_exhausted", "configured GitHub request budget reached")


@dataclass
class Response:
    data: object
    link: str = ""
    cached: bool = False
    deferred: bool = False
    received_count: int | None = None


class NoRedirect(request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # Prevent forwarding authentication to a redirect, including repository moves.
        return None


def auth_token():
    for key in ("PAKURI_GITHUB_TOKEN", "GH_TOKEN", "GITHUB_TOKEN"):
        if os.environ.get(key):
            return os.environ[key]
    try:
        result = subprocess.run(["gh", "auth", "token", "--hostname", "github.com"], capture_output=True, text=True, timeout=5, check=False)
        return result.stdout.strip() if result.returncode == 0 else None
    except (OSError, subprocess.TimeoutExpired):
        return None


def checked_path(path):
    parsed = parse.urlsplit(path)
    if parsed.scheme or parsed.netloc or parsed.fragment or not path.startswith("/"):
        raise ValueError("only relative api.github.com endpoints are allowed")
    parts = parsed.path.split("/")
    allowed = False
    if len(parts) in (4, 5) and parts[1] == "users" and valid_login(parts[2]):
        allowed = parts[3:] == ["events", "public"] or parts[3:] == ["repos"]
    if len(parts) == 4 and parts[1] == "orgs" and valid_login(parts[2]):
        allowed = parts[3] in {"events", "repos"}
    if len(parts) in (4, 5) and parts[1] == "repos" and valid_repo("/".join(parts[2:4])):
        allowed = len(parts) == 4 or (len(parts) == 5 and parts[4] in {"commits", "releases"})
    query = parse.parse_qs(parsed.query, strict_parsing=True)
    if not allowed or not set(query) <= {"page", "per_page", "type", "sort", "direction", "since", "sha"}:
        raise ValueError("unsupported GitHub endpoint or query")
    if any(len(values) != 1 or len(values[0]) > 200 for values in query.values()):
        raise ValueError("invalid GitHub query")
    return path


class GitHubClient:
    def __init__(self, state, *, max_requests=300, reserve=20, timeout=15, token=None, now=None, opener=None):
        self.state = state
        self.max_requests, self.reserve, self.timeout = max_requests, reserve, timeout
        self.token = token if token is not None else auth_token()
        self.now = now or (lambda: datetime.now(timezone.utc))
        self.opener = opener or request.build_opener(NoRedirect)
        self.requests = 0
        self.cache_hits = 0
        self.rate = {}
        self.stop_reason = None

    def _headers(self, headers):
        for raw, key in (("X-RateLimit-Remaining", "remaining"), ("X-RateLimit-Limit", "limit"), ("X-RateLimit-Reset", "reset"), ("X-RateLimit-Used", "used")):
            try:
                if headers.get(raw) is not None:
                    self.rate[key] = int(headers.get(raw))
            except (TypeError, ValueError):
                pass
        self.rate["resource"] = headers.get("X-RateLimit-Resource", "core")

    def get(self, path):
        checked_path(path)
        if self.stop_reason:
            raise GitHubError(self.stop_reason, "GitHub polling paused for this run")
        cache = self.state.cache_get(path)
        moment = self.now()
        if cache and timestamp(cache.get("next_poll")) and moment < timestamp(cache["next_poll"]):
            self.cache_hits += 1
            return Response(cache["data"], cache["link"] or "", cached=True, deferred=True)
        if self.requests >= self.max_requests:
            raise BudgetExhausted()
        if self.rate.get("remaining", self.reserve + 1) <= self.reserve:
            self.stop_reason = "rate_reserve"
            raise GitHubError("rate_reserve", "GitHub rate limit reserve reached")
        headers = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": API_VERSION, "User-Agent": "Pakuri/0.1 public-metadata-observer"}
        if self.token:
            headers["Authorization"] = "Bearer " + self.token
        if cache and cache.get("etag"):
            headers["If-None-Match"] = cache["etag"]
        req = request.Request("https://api.github.com" + path, headers=headers, method="GET")
        self.requests += 1
        try:
            response = self.opener.open(req, timeout=self.timeout)
            with response:
                self._headers(response.headers)
                raw = response.read(8 * 1024 * 1024 + 1)
                if len(raw) > 8 * 1024 * 1024:
                    raise GitHubError("response_too_large", "GitHub metadata response exceeded size cap")
                decoded = json.loads(raw)
                received_count = len(decoded) if isinstance(decoded, list) else None
                data = project(path, decoded)
                return self._cache_response(path, response.headers, data, cache, moment, received_count=received_count)
        except error.HTTPError as exc:
            self._headers(exc.headers)
            if exc.code == 304 and cache:
                self.cache_hits += 1
                return self._cache_response(path, exc.headers, cache["data"], cache, moment, cached=True)
            secondary = False
            if exc.code == 403:
                try:
                    message = json.loads(exc.read(4096)).get("message", "")
                    secondary = "rate limit" in str(message).lower()
                except (ValueError, OSError, AttributeError):
                    pass
            if exc.code in (403, 429) and (exc.code == 429 or exc.headers.get("Retry-After") or self.rate.get("remaining") == 0 or secondary):
                self.stop_reason = "rate_limited"
                retry = safe_title(exc.headers.get("Retry-After"), "")[:40] or None
                raise GitHubError("rate_limited", "GitHub rate limit response; no automatic sleep", retry_after=retry) from None
            # Never include response body, URL, tokens, headers or exception prose.
            raise GitHubError("http_" + str(exc.code), "GitHub HTTP " + str(exc.code)) from None
        except (error.URLError, TimeoutError, OSError):
            raise GitHubError("network_error", "GitHub request failed or timed out") from None
        except (ValueError, TypeError, UnicodeError):
            raise GitHubError("invalid_response", "GitHub metadata response is invalid") from None

    def _cache_response(self, path, headers, data, previous, moment, *, cached=False, received_count=None):
        link = headers.get("Link") or (previous.get("link") if cached and previous else "") or ""
        poll = None
        try:
            seconds = int(headers.get("X-Poll-Interval", "0"))
            if seconds > 0:
                poll = utc(moment + timedelta(seconds=min(seconds, 86400)))
        except (ValueError, TypeError):
            pass
        etag = headers.get("ETag") or (previous.get("etag") if previous else None)
        self.state.cache_put(path, data, etag, link, utc(moment), poll)
        return Response(data, link, cached=cached, received_count=received_count)
