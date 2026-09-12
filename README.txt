WINGO PRO FINAL

- bot.py is the consolidated Telegram bot.
- index.html uses the bot's /api/latest endpoint; it never calls the WinGo API directly.
- API engine races the official endpoint and fallback gateways with short timeouts.
- API response parsing supports data.list, data[], list[], records[], items[], rows[] and nested JSON.
- Latest issue is selected by the highest numeric issue number instead of blindly trusting array order.
- Bot sends ONLY the next Issue/Period number (+1). The result Number is never sent.
- Failed upstream API calls are logged server-side; proxy URLs/errors are never sent to members.
- A successful issue is cached so a temporary 403/timeout does not break the user flow.
- Request button supports visible custom text plus a hidden Telegram /start payload.
- Existing SQLite/GitHub member/state persistence, join requests, approval flow, admin replies and menus are retained.

IMPORTANT:
The upstream draw.ar-lottery01.com server can still block a Render/datacenter IP with HTTP 403. No Python header can guarantee bypass of an upstream firewall. This build handles that failure cleanly and uses fallbacks/cache, but a live upstream source must be reachable for a fresh issue.
