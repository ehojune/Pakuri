# Validation (lookup only)

Checked 2026-10-01 on the owner's Windows PC.

| Check | Result |
|---|---|
| Identity roster | 34 people (30 additions), 16 organizations, 44 public curated repositories; 17 current affiliations unconfirmed |
| Pakuri tests | 41 passed: baseline/delay/recovery/dedup, request/page/rate limits, privacy, locks/publication, output cap, advancing clocks and cached metadata |
| Kuromi adapter tests | 24 passed with fake Slack; real SDK construction and tool handler also passed without model connection |
| First live collection | `ok`, 815 requests, 640 successful source scopes, 10,386 stored baseline observations, 0 new activity candidates, 0 errors |
| Bounded JSON | 1,000 items, 802,857 bytes; 9,386 observations omitted from JSON but retained in SQLite |
| Known initial exclusions | 3,037 enumerated owned repositories outside the selected polling cap; 2 invalid records skipped |
| Concurrent manual collection | While scheduled collector held the lock: `writer_busy`, exit 2, no competing JSON published |
| Private GitHub | `ehojune/Pakuri` created and `isPrivate=true` confirmed |
| Local schedule | `Pakuri-Collect`: hourly + user logon; first scheduled run 04:53–05:02 KST, exit 0, `ok`, 818 requests, 648 successful scopes, 440 cache hits, 0 errors; next run 05:51 KST |
| Repeat observation | 10,547 stored observations; 18 new activity candidates after reconciling 2 initially observed commits affected by the old clock. No duplicate IDs or reader rejection |
| Kuromi activation | Local `.env` PAKURI_PATH set; existing app restarted hidden at 04:54 KST after checking no active model child; new process startup found, no error traceback |
| Outbound data check | Tracked files scanned; no credential patterns, state DB, raw observation JSON or logs included |
| CI | Windows and Ubuntu jobs passed; [runs](https://github.com/ehojune/Pakuri/actions) |
| Read-often docs | New project: 0 → 2,855 characters total (README 1,956; NEXT 318; DECISIONS 581). Reference evidence excluded |

The private roster and data remain in Pakuri. Kuromi's public checkout contains only generic adapter/config hooks; its other uncommitted changes were preserved and were not pushed. `.env` activation and app restart are local only. A pre-restart log and environment backup remain in this chat's `work/`.

Limits: actual Slack briefing/model generation has not been sent as a test. The next scheduled briefing will load the new adapter. Slack acknowledgement and local ledger persistence cannot be one transaction: a ledger-write failure after successful delivery may repeat an item later. GitHub coverage is intentionally bounded, not a complete history. Hourly execution needs this PC on and the owner signed in. The collector preserves compact metadata indefinitely; its obsolete HTTP cache is pruned after seven days.

Independent review's two defects were fixed with regression tests: repository classifications survive another poll; publication remains inside the same process lock. Windows locked-byte initialization was also reproduced and repaired.

The initial collector used its start clock throughout a long run and skipped 2 commits created during collection. Production now uses live clocks, source start/completion checkpoints and a completion-time window. Advancing-clock tests pass. Only the two evidenced first-run commits were reconciled to baseline in local state; their metadata remains intact. This clock correction was validated with deterministic advancing-clock tests after both complete live runs; the next hourly run uses the corrected code.
