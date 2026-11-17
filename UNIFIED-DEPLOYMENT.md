# KIWI + Verification: one service

This branch adds a unified launcher while leaving the existing bot and verification repositories unchanged.

## Start command

```bash
python unified_launcher.py
```

## Required environment variables

- `DISCORD_TOKEN` — KIWI Discord bot token.
- `MONGODB_URI` — existing MongoDB Atlas connection string.
- `MONGODB_DATABASE` — KIWI database name (normally `cedar_cove`).
- `STAFF_ACCOUNT_DELETE_SECRET` — same secret used by the staff dashboard's protected account-management routes.
- `PORT` — public port supplied by the hosting platform.

Optional:
- `BOT_INTERNAL_PORT` — defaults to `18081`.
- `VERIFY_INTERNAL_PORT` — defaults to `18082`.
- `CLIENT_IP_HEADER` — defaults to `x-forwarded-for` for rate-limit IP detection behind the public proxy.

## Routing

The public listener forwards:
- `/api/auth/*` and `/api/internal/staff/*` to the verification API.
- Dashboard, staff, appeals, and other bot routes to KIWI's existing aiohttp server.
- `/health` returns a combined status for both components.

The internal listeners bind only to `127.0.0.1`; only the public proxy binds to `0.0.0.0`.

## Database separation

The verification API retains its existing MongoDB database selection (normally `authdb`). KIWI continues using `MONGODB_DATABASE` (normally `cedar_cove`). No database migration or collection copying is performed.

## Rollout

Deploy this branch as a test service first. Keep the existing EthioDeploy KIWI service and Render verification service untouched until login, signup, dashboard access, staff routes, and Discord connectivity have all been tested.
