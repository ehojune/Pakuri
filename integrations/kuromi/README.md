# Kuromi adapter

This adapter and its fixture tests match the local Kuromi installation. They contain no target roster or credentials.

- Install `pakuri_tools.py` beside Kuromi's `brain.py`.
- Hook `build_pakuri_tool` and `post_briefing` as shown in [the additive patch](../../docs/reference/kuromi.patch).
- Set `PAKURI_PATH` only in local `.env`; keep it blank in public examples.
- Do not apply the patch again to this PC: the local checkout already has these changes.
- Run `python -m unittest discover -s integrations/kuromi -v` from Pakuri to test delivery without Slack.
