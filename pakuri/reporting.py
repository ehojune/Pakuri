"""Small evidence-first reports. External strings are inert, escaped data."""
from collections import defaultdict


TOPICS = {
    "agents": "AI agent", "ai-agents": "AI agent", "agent": "AI agent",
    "evaluation": "평가", "workflow": "워크플로", "workflows": "워크플로",
    "genomics": "유전체", "bioinformatics": "생물정보학",
    "biology": "생물학", "single-cell": "단일세포", "protein": "단백질",
}
KINDS = {"release": "릴리스", "commit": "커밋", "push": "push", "new_repo": "새 저장소", "repository": "저장소", "repo_created": "새 저장소"}
PRIORITY = {"release": 0, "new_repo": 1, "repository": 1, "repo_created": 1, "push": 2, "commit": 3}


def safe_text(value, limit=160):
    """Prevent Markdown/Slack control text and mentions; never interpret titles."""
    text = " ".join(str(value or "").split())[:limit]
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace("`", "'").replace("[", "(").replace("]", ")")


def select_items(payload, limit=8):
    """Importance precedes personal relevance; spread space across repositories."""
    candidates = [x for x in payload.get("items", []) if not x.get("baseline", True)]
    candidates.sort(key=lambda x: x.get("published_at", ""), reverse=True)
    candidates.sort(key=lambda x: PRIORITY.get(x.get("kind"), 4))
    per_repo = defaultdict(int)
    chosen = []
    for item in candidates:
        repo = item.get("repo", "")
        if per_repo[repo] >= 2:
            continue
        per_repo[repo] += 1
        chosen.append(item)
        if len(chosen) >= limit:
            break
    return chosen


def render(payload, limit=8):
    if payload.get("schema_version") != 1:
        raise ValueError("Unsupported schema_version")
    stamp = safe_text(payload.get("generated_at"), 32)
    status = safe_text(payload.get("status", "error"), 16)
    lines = [f"Pakuri · {stamp} · {status}"]
    items = select_items(payload, max(1, min(limit, 50)))
    if status != "ok":
        lines.append("수집 제한·실패가 있습니다. coverage와 errors에서 범위를 확인하세요.")
    if not items:
        baselines = sum(bool(x.get("baseline")) for x in payload.get("items", []))
        lines.append("첫 관측을 기준선으로 저장했습니다." if baselines else "이 관측 범위에서 새 활동이 없습니다.")
        return "\n".join(lines) + "\n"
    groups = defaultdict(int)
    for item in items:
        topics = item.get("topics", [])
        topic = topics[0] if topics else "개발 도구"
        groups[TOPICS.get(topic, safe_text(topic, 30))] += 1
    lines.append("분야 관측: " + " · ".join(f"{k} {v}건" for k, v in groups.items()) + " (선정 활동 수)")
    lines.append("제목만으로 성능 개선이나 분야 전체 추세를 단정하지 않습니다.")
    for item in items:
        repo = safe_text(item.get("repo"), 100)
        kind = KINDS.get(item.get("kind"), safe_text(item.get("kind"), 24))
        title = safe_text(item.get("title"))
        published = safe_text(item.get("published_at"), 32)
        lines.append(f"\n- 사실: {repo} · {kind} · {published} · {title}")
        url = item.get("url", "")
        # Collector validates URLs; keep render defensive for imported fixtures.
        from urllib.parse import urlsplit
        parsed = urlsplit(url)
        if parsed.scheme == "https" and parsed.netloc == "github.com" and not any(c in url for c in "\n\r<> "):
            lines.append(f"  원문: {url}")
        suggestion = "변경·호환성·검증 기록을 살펴보고 도입 여부를 판단하세요." if kind != "새 저장소" else "문제 설정과 개발 구조를 살펴보고 재현 예제를 확인하세요."
        lines.append(f"  참고 제안: {suggestion}")
        relevance = item.get("relevance", [])
        if relevance:
            lines.append("  내 프로젝트 참고 후보: " + ", ".join(safe_text(x, 40) for x in relevance[:4]))
    return "\n".join(lines) + "\n"
