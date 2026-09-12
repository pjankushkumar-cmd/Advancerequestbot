# Telegram PRO Bot — Render

## Render
Build:
`pip install -r requirements.txt`

Start:
`python hackiirequestbot_PRO_FINAL.py`

## Environment Variables
- BOT_TOKEN
- ADMIN_ID
- GITHUB_TOKEN
- GITHUB_OWNER = pjankushkumar-cmd
- GITHUB_REPO = bypasshackiibot
- GITHUB_FILE = members.json
- GITHUB_STATE_FILE = bot_state.json

## Persistence
`members.json` stores member IDs.
`bot_state.json` stores bot settings and Telegram message references.

## Important
Telegram message references are persisted, but the original Telegram messages must remain accessible to the bot for `copy_message` to work.
