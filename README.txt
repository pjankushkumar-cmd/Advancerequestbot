WIN GO BOT FULL UPDATED

1. The bundled index.html uses the Python /api/latest endpoint, so the browser
   does not call draw.ar-lottery01.com directly. This avoids browser CORS/403.
2. The Python API engine tries the official endpoint first, then fallback
   gateways if the source blocks the request.
3. On Start flow, the bot sends ONLY the next Issue/Period number calculated
   from the latest API issue (+1). The result Number is not shown.
4. Request button can be configured with any visible text. When Action=START,
   the button becomes a Telegram deep link and the /start payload is hidden
   behind the button. Configure it from Admin -> Request Settings -> Start Payload.
5. Existing SQLite/GitHub persistence, join requests, admin panel, approval
   message, replies, and other existing features are retained.
6. For the Start API message to be sent, API must be ON in Admin -> API Settings.
   API After N=0 means after the configured Start sequence.
