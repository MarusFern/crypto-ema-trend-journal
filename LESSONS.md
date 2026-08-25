# Lessons

Advisory observations only — see README.md for the rules governing this file.
The strategy's hard rules are fixed in the routine prompt and are not to be changed
based on entries here without explicit human review.

- 2026-08-25: The Alpaca MCP connector has intermittently failed to attach to the session on 4 of ~9 hourly runs today (11:56, 12:56, 16:07, 16:56 UTC), despite ListConnectors always reporting installState=connected/enabledInChat=true at the org level — the actual tool set is simply absent from the session (confirmed via ListMcpResourcesTool/ToolSearch). It has self-resolved on the next run each time so far without human intervention, but this means position monitoring (stops, trend exits) has real, recurring multi-hour gaps. Human should investigate the root cause of the connector attach flakiness; this is a platform/session reliability issue, not a strategy issue, and no guardrail changes are implied.
