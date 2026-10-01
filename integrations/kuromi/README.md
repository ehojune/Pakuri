# Kuromi adapter

This is a standalone reference adapter with offline fixture tests, not a replacement for an extended Kuromi installation. It contains no target roster or credentials.

- Install `pakuri_tools.py` beside Kuromi's `brain.py`.
- Hook `build_pakuri_tool` and `post_briefing` as shown in [the additive patch](../../docs/reference/kuromi.patch).
- Set `PAKURI_PATH` only in local `.env`; keep it blank in public examples.
- Current Kuromi also has independent news/context extensions. Compare its adapter before integrating; do not overwrite it with this example or reapply the patch to this PC.
- Run `python -m unittest discover -s integrations/kuromi -v` from Pakuri to test delivery without Slack.
