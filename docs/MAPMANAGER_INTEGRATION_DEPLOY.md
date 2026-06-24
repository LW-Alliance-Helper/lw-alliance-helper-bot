# Map Manager integration — bot deploy notes (#316)

The bot exposes an inbound HTTP API (`api_server.py`) that Map Manager (MM)
calls. This changes how the bot is deployed on Railway. The code change rides
this branch; the Railway-side validation below is the collaborator's (the bot
session can't test Railway).

## What changed in this branch

- **`Procfile`: `worker` → `web`.** A Railway `worker` process gets **no inbound
  HTTP routing**, so MM's calls would never reach the bot. The API server must
  run **in the same process as the gateway** (the `/api/guilds/:id/members/:uid`
  lookup reads the live gateway member cache), so it can't be split into a
  separate web service. Therefore the single bot process becomes the `web`
  service.
- **The server binds `$PORT` on a web deploy even before the key is set.**
  `api_server_enabled()` now returns true when `PORT` is set (Railway web) OR
  `MAPMANAGER_API_KEY` is set. This means a web deploy binds the port (so
  Railway's routing + health check pass) **before** the integration is
  configured; the uncredentialed `/healthz` answers the health check, and
  credentialed routes return 503 until `MAPMANAGER_API_KEY` is set. This avoids
  a deploy-ordering trap (switching to `web` without the key would otherwise
  leave nothing on `$PORT` and fail the health check).

## Collaborator checklist (Railway)

1. **Service type.** Confirm the bot's Railway service is set up to expose HTTP
   (a public domain / port), now that the Procfile process is `web`. Confirm the
   gateway (Discord) connection still works as a web service.
2. **Env vars** (per environment): set `MAPMANAGER_API_KEY` (the per-env service
   key generated at MM's `/admin/api-keys`) and `MAPMANAGER_API_URL` (MM base
   URL). `PORT` is provided by Railway. The same key is presented in both
   directions.
3. **Health-check window.** The HTTP server starts in `on_ready` — i.e. after
   the gateway logs in (a few seconds into boot), not at process start. If
   Railway's health check has a tight timeout, give it a generous one so the
   deploy isn't marked unhealthy during the gateway-login window. (If this turns
   out to be a problem in practice, the fix is to bind the port earlier in
   startup, before the gateway READY.)
4. **Reachability smoke test.** `curl https://<bot-web-url>/healthz` → `200
   {"status":"ok"}`. Then with the service key:
   `curl -H "Authorization: Bearer <key>" https://<bot-web-url>/api/guilds/<id>/link`.

## After deploy: end-to-end verification (handoff §0.8 / §5)

`/map_manager setup` from a test guild → sign into MM via Discord → alliance
pages populate from the bot reads (`/sheet/roster`, `/sheet/growth`) → post a
plan from MM and confirm the rows land in the storm `rosters_tab`
(`POST /sheet/storm-roster`, the one write path whose live I/O is not covered by
unit tests). Revoke the key at `/admin/api-keys` and confirm the next bot call
returns 401.
