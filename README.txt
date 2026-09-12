WinGo PRO - API REMOVED

This version intentionally contains NO WinGo/lottery result API system.

Render:
- Build command: pip install -r requirements.txt
- Start command: python bot.py
- Procfile: web: python bot.py

Environment variables:
- BOT_TOKEN (required)
- ADMIN_ID (required)
- GITHUB_TOKEN (optional but recommended)
- GITHUB_OWNER (optional)
- GITHUB_REPO (optional)
- GITHUB_FILE (optional, default members.json)
- GITHUB_STATE_FILE (optional, default bot_state.json)
- PORT (optional, Render supplies it)

Admin:
- /start or /admin -> admin panel
- /sync_members -> sync members/state
- /broadcast -> fast broadcast mode

Features:
- Request messages + customizable button text/action/deep-link payload
- Start message sequence + configurable final message
- Approval message + button + enable/disable
- Every member reply after the configured final Start message is forwarded to ADMIN_ID
- Admin can reply to the specific member from the reply button
- Fast bounded-concurrency broadcast to all saved members
- GitHub persistence for members and settings
- Render health server at / and /health
