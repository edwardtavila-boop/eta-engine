# Hermes Multi-Channel Setup (Track 5)

Onboarding checklist for adding Discord, Slack, iMessage, and webhook
delivery to the Hermes Agent running on the VPS. Telegram is already
configured; this doc covers everything else.

## Order of operations

1. Read this doc end-to-end.
2. Pick which channels you actually want. Each costs ~10 minutes of setup.
3. For each chosen channel, follow its section below to obtain tokens.
4. Add the tokens to `eta_engine/deploy/hermes_secrets.bat` (gitignored sidecar
   that `hermes_run.bat` sources at gateway start).
5. Copy the relevant platform block from `hermes_multichannel.example.yaml`
   into `~/.hermes/config.yaml` on the VPS (under existing `platforms:` key).
6. Restart `ETA-Hermes-Agent` scheduled task.
7. Test by sending a message from each channel to the bot.

## 1. Telegram (already on)

Working as of 2026-05-12 — no action required. Briefings already fire
to your configured chat ID at 06:30 UTC.

If you want to add more chat IDs to the allowlist, edit
`TELEGRAM_ALLOWED_USERS` in `hermes_secrets.bat`.

## 2. Discord — recommended for mobile pocket UX

**Why:** Best phone experience for "ask JARVIS a question while you're out".
Discord push notifications are reliable, the bot can post inline embeds
with charts, and free tier is generous.

**Setup (~15 min):**

1. Go to https://discord.com/developers/applications → "New Application"
2. Name it "JARVIS Hermes" (or whatever).
3. Sidebar → "Bot" → "Reset Token" → copy the token.
4. Settings → check "Message Content Intent" and "Server Members Intent".
5. Add to `hermes_secrets.bat`:
   ```bat
   set DISCORD_BOT_TOKEN=<paste-token-here>
   ```
6. OAuth2 → URL Generator → check scopes: `bot`, `applications.commands`.
   Bot permissions: Send Messages, Read Message History, Embed Links.
7. Copy the generated URL, open it in your browser, invite to your server.
8. In Discord, enable Developer Mode (User Settings → Advanced) and copy
   the channel ID where you want default delivery (right-click channel
   → Copy ID).
9. In `~/.hermes/config.yaml`, add the `discord:` block from
   `hermes_multichannel.example.yaml`. Set `enabled: true` and replace
   `default_channel_id`.
10. Restart `ETA-Hermes-Agent` task.

**Test:** message your bot in the configured Discord channel with "ping" —
should reply within ~3s.

## 3. Slack — recommended for team / multi-operator

**Why:** Use this instead of (or alongside) Discord if you work with a
co-trader or assistant, or if you want a more structured/business feel.

**Setup (~20 min):**

1. Go to https://api.slack.com/apps → "Create New App" → "From scratch".
2. Name "JARVIS Hermes", pick your workspace.
3. Sidebar → Socket Mode → enable → create an App-Level Token with
   scope `connections:write`. Copy `xapp-…` token.
4. OAuth & Permissions → Bot Token Scopes, add: `chat:write`,
   `channels:history`, `groups:history`, `im:history`, `mpim:history`,
   `app_mentions:read`.
5. Install App to Workspace. Copy the Bot User OAuth Token (`xoxb-…`).
6. Add to `hermes_secrets.bat`:
   ```bat
   set SLACK_BOT_TOKEN=xoxb-...
   set SLACK_APP_TOKEN=xapp-...
   ```
7. Event Subscriptions → enable. Subscribe to bot events: `message.channels`,
   `message.groups`, `message.im`, `app_mention`.
8. In Slack, invite the bot to the channel you want as default
   (`/invite @JARVIS Hermes`).
9. In `~/.hermes/config.yaml`, add the `slack:` block; set `enabled: true`
   and replace `default_channel` with your channel name (with `#`).
10. Restart `ETA-Hermes-Agent`.

**Test:** mention the bot in your Slack channel with `@JARVIS Hermes ping` —
should reply.

## 4. iMessage via BlueBubbles — Apple-ecosystem operators

**Why:** Lets you text the bot from iMessage on your iPhone / Mac. Most
seamless if you're already deep in the Apple stack.

**Requires:**

* A Mac you control (always-on). This is the BlueBubbles **server**.
  Apple's Messages app on Mac is the actual iMessage endpoint; BlueBubbles
  wraps it in an HTTP API.
* A way to reach that Mac from the VPS — Tailscale is easiest, ngrok works.

**Setup (~30 min, mostly on the Mac):**

1. Download BlueBubbles Server from https://bluebubbles.app/server/
2. Install on the always-on Mac. Grant it Full Disk Access + Accessibility
   permissions (it scripts the Messages app).
3. Set a strong password in BlueBubbles Server.
4. Set up Tailscale (recommended) or ngrok on the Mac so the VPS can reach
   `http://mac:1234/api/v1/...`
5. Add to `hermes_secrets.bat`:
   ```bat
   set BLUEBUBBLES_API_URL=https://<your-tunnel-hostname>
   set BLUEBUBBLES_PASSWORD=<the password you set>
   ```
6. In `~/.hermes/config.yaml`, add the `bluebubbles:` block; set
   `enabled: true` and replace `default_chat_guid` with
   `iMessage;-;+1XXXYYYZZZZ` (your phone number).
7. Restart `ETA-Hermes-Agent`.

**Test:** text "ping" to the bot's iMessage number → should reply.

**Caveat:** iMessage delivery is slower than Discord/Slack (5–15s typical)
because BlueBubbles polls the local Messages.app database. Use it for
casual chat, not for time-sensitive trading alerts (use Telegram or Discord
for those).

## 5. Generic webhook (Claw3D / dashboards / inter-agent)

**Why:** Push events from Hermes to ANY external service that accepts
HTTP POST — Claw3D, Grafana, custom dashboards, another AI agent.

**Outbound only** (Hermes → external): use the `delivery: webhook` config
on a scheduled task. Example:

```yaml
scheduled_tasks:
  - name: kaizen_action_to_claw3d
    cron: "*/15 * * * *"   # every 15 min
    delivery: webhook
    delivery_extra:
      url: "http://127.0.0.1:8765/hermes/events"
      method: POST
    prompt: |
      Call jarvis_subscribe_events(stream="kaizen", since_offset=<cursor>,
      limit=10). If any new entries, return their JSON list. Otherwise
      return empty string (suppresses delivery).
```

**Inbound** (external → Hermes): enable the `webhook:` platform per the
example yaml. Run with HMAC secret in production.

**Claw3D wiring:** Claw3D / Hermes Office already connects to the local
api_server on port 8642 by default. To get JARVIS-specific events on the
Claw3D timeline, two paths:

1. **Polling path (simplest):** add a JS snippet inside Claw3D that polls
   `GET /v1/jarvis-events?since=<offset>` (a route you'd add to the
   api_server) every 5s. No webhook needed.

2. **Push path:** add a webhook scheduled task per the snippet above and
   point it at a local URL Claw3D listens on. Faster than polling but
   needs Claw3D to expose an inbound port.

Pick (1) for v1; switch to (2) if you find the polling lag annoying.

## Verifying multi-channel fan-out

Once two or more channels are enabled, edit
`scheduled_tasks.morning_briefing.delivery` to a list:

```yaml
- name: morning_briefing
  cron: "30 6 * * *"
  delivery: [telegram, discord, slack]
  prompt: |
    Run jarvis_fleet_status and jarvis_wiring_audit. Render a 5-line
    morning briefing.
```

Tomorrow at 06:30 UTC, the same briefing fires to every listed channel.

## Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| Bot doesn't reply | Token in `hermes_secrets.bat` not loaded | `schtasks /Run /TN ETA-Hermes-Agent` |
| Bot replies once then dies | App-Level Token missing (Slack) | Verify both `SLACK_BOT_TOKEN` + `SLACK_APP_TOKEN` are set |
| Discord bot offline | Bot not invited to server, or wrong intent flags | Re-invite via OAuth2 URL; enable Message Content Intent |
| iMessage replies are 30s late | BlueBubbles poll_interval too low for your hardware | Increase `poll_interval_seconds` to 10 |
| Multi-channel only fires one | Delivery list YAML syntax wrong | Use `[telegram, discord]` not `telegram, discord` |
