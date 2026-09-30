# Consumer contract (lookup only)

The command is unchanged:

```powershell
python -m pakuri collect --config targets.json --output data/latest.json --hours 24
```

| JSON field | Meaning |
|---|---|
| schema_version | 1 |
| generated_at | UTC ISO completion clock for this collection snapshot |
| window_hours | Requested rolling observation window |
| status | Collector health; consumers must display partial/failure/stale state |
| items | Compact public metadata, persistent stable IDs |
| errors | Sanitized source failures; never credentials |
| coverage | Sources, pagination, budget, checkpoint and known blind spots |

Every item has `id, kind, title, url, published_at, observed_at, actor, repo, baseline, topics, relevance`. `actor`/`repo` are strings; `topics`/`relevance` are string lists. `baseline:true` is an initial observation and is excluded from new-activity reports. A source first added later also establishes its own baseline.

Publication time means the source's event, commit or release time; it is not interchangeable with observation time. A commit timestamp alone does not establish when a branch was pushed. A repository creation time does not establish when an account acquired it.

Concrete connection changes: (1) Pakuri collects hourly into its own ignored SQLite/JSON files; (2) `PAKURI_PATH` selects that local folder; (3) `pakuri_activity` reads the cache without network calls or delivery mutations; (4) the morning briefing appends a deterministic brief and records only included IDs after Slack acknowledges success. No private target roster is copied into Kuromi.

External title/message text is escaped, length-limited data. The tool envelope labels it untrusted. No external text is used as a shell command, path, prompt instruction or URL to another host.

GitHub event limits and delivery latency are documented by [GitHub](https://docs.github.com/en/rest/activity/events). API quota/backoff guidance: [rate limits](https://docs.github.com/en/rest/using-the-rest-api/rate-limits-for-the-rest-api), [best practices](https://docs.github.com/en/rest/using-the-rest-api/best-practices-for-using-the-rest-api). Commit polling covers only the selected default branch; configured owner/repository caps and fetch failures remain explicit. `ok` means attempted bounded sources succeeded, not complete observation of GitHub.
