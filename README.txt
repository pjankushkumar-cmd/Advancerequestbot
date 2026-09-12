WinGo Bot Fixed Final

1. Set BOT_TOKEN and ADMIN_ID.
2. Optional GitHub variables remain supported.
3. The WinGo API is built in and tries the direct endpoint first, then fallback fetchers.
4. API failures are logged instead of being sent repeatedly to users.
5. On /start, the latest issue is read and the next issue (+1) is sent. Result number is never shown.
6. Request button can use a hidden Telegram /start deep-link payload configured from Request Settings.
7. index.html is served with the bot health server and reads /api/latest, so the browser does not call the lottery API directly.
