# Kuromi integration evidence

- Implemented in the local Kuromi checkout: `pakuri_tools.py`, `tests/test_pakuri_tools.py`; narrow additions to `config.py`, `brain.py`, `daily_briefing.py`, `.env.example`.
- `PAKURI_PATH` is optional and blank in the public example. Only cached `data/latest.json` is read; collection is scheduled by Pakuri.
- `pakuri_activity` returns bounded untrusted metadata with UTC/schema/source URL checks. Queries never read or consume the delivery ledger.
- The morning section lists observed fields and changes, dated source facts, a labeled reference suggestion, then local relevance. It is assembled deterministically after the existing Brain response.
- Only IDs actually included are acknowledged after Slack returns `ok=true`. Delivery uses an OS-held lock and atomic, bounded local `data/kuromi-delivery.json`. Keep `data/` ignored. Old ledger entries expire after 30 days.
- A failed/partial/stale/initial baseline is stated honestly; initial baseline items are not reported as new. Delayed observations retain the original publication date. Slack mentions and linebreaks in external titles are escaped.
- 2026-10-01: 24 fixture tests passed (`.venv\Scripts\python.exe -m unittest discover -s tests -p test_pakuri_tools.py`). Cases cover duplicate delivery, baseline, stale data, partial/error handling, delayed discovery, release priority, hostile URLs/titles, Slack failure/false result, locks, and ledger failure.
- Actual SDK Brain construction and tool handler were exercised without login, client connection, or network. Python compilation and `git diff --check` passed.
- Local PAKURI_PATH was activated and Kuromi restarted hidden at 04:54 KST. The actual SDK lists `pakuri_activity`; its reader accepts the live JSON without rejected items or delivery-ledger changes. Existing uncommitted edits and AI-news work were preserved; no Kuromi commit/push or Slack test message was sent. Scheduled morning runs load the adapter on their next invocation.
- `kuromi.patch` contains only the additive integration against the pre-edit working tree, with no roster, local configuration value, or secret. It is an audit/export patch; do not apply it again to the already modified local checkout.
- The adapter under `integrations/kuromi/` is a tested standalone reference. Current Kuromi has separate news/context additions; preserve them rather than copying this snapshot over its adapter.
- Limitation: Slack and a local ledger cannot form one transaction. If Slack accepts a message but ledger saving fails, the error is logged without another post; the activity may be repeated next time.
