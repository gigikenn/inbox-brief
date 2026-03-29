# Outlook AI Email Briefing

**Deployed API (this project):** [https://inbox-brief.onrender.com](https://inbox-brief.onrender.com) — mobile shell: `/mobile`, spoken brief: `/digest/spoken`.

Small FastAPI service that:
- Reads unread Outlook inbox emails
- Ignores senders/domains you define
- Uses OpenAI (batched per run) for **short AI summaries** + action + priority
- Returns a sorted digest and a Siri-friendly spoken string

## 1) Prerequisites

- Python 3.10+
- Outlook/Microsoft account
- OpenAI API key
- Azure App Registration (for Microsoft Graph)

## 2) Azure Setup (Microsoft Graph)

1. Go to [Microsoft Entra admin center](https://entra.microsoft.com/) -> **App registrations** -> **New registration**.
2. Name it something like `Outlook AI Brief`.
3. Supported account type: choose the one that matches your account (for personal Outlook, start with multi-tenant + personal).
4. Register app.
5. Copy the **Application (client) ID**.
6. Go to **API permissions** -> **Add a permission** -> **Microsoft Graph** -> **Delegated permissions**:
   - `Mail.Read`
   - `User.Read`
7. Save.

This app uses **device code login** (no redirect URI needed for MVP).

## 3) Local Setup

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

Edit `.env`:
- `MS_CLIENT_ID` = your Azure app client ID
- `OPENAI_API_KEY` = your OpenAI key
- optionally tweak `IGNORE_SENDERS`, `IGNORE_DOMAINS`, `VIP_SENDERS`

## 4) Run

```bash
uvicorn app:app --reload
```

Server defaults to `http://127.0.0.1:8000`.

## 5) Authenticate Outlook (device flow)

Start auth:

```bash
curl -X POST http://127.0.0.1:8000/auth/start
```

This returns a `message` with a URL + code. Complete sign-in in browser, then finish:

```bash
curl -X POST http://127.0.0.1:8000/auth/complete
```

## 6) Get Digest

JSON output:

```bash
curl "http://127.0.0.1:8000/digest"
```

If you set `DIGEST_ACCESS_KEY` in `.env`, append `?access_key=YOUR_SECRET` to digest URLs.

Siri-friendly one-line spoken text:

```bash
curl "http://127.0.0.1:8000/digest/spoken"
```

## 7) iPhone Shortcut (Speak it)

Create an iOS Shortcut named `Inbox Brief`:
1. Action: **Get Contents of URL**
   - URL: your endpoint for `/digest/spoken`
   - Method: GET
2. Action: **Speak Text**
   - Text input = result from URL action

Then say: **"Hey Siri, Inbox Brief"**.

If your server is local-only, your phone must be on the same network and your machine must expose the endpoint (LAN IP, optional tunnel, etc).

## 8) iPhone “app” (Add to Home Screen)

The server serves a full-screen **mobile shell** at **`/mobile`** (not a generic web “site” — one screen, big button, optional read-aloud).

1. On your Mac, listen on all interfaces so the phone can reach it:
   ```bash
   uvicorn app:app --host 0.0.0.0 --port 8000
   ```
2. On the iPhone (same Wi‑Fi), open Safari:
   `http://YOUR_MAC_LAN_IP:8000/mobile`
3. Tap **Share** → **Add to Home Screen** → name it **Inbox Brief**.
4. Open it from the home screen — it launches like an app. Tap **Brief my inbox**; use **Read aloud** if you want voice.
5. If you use `DIGEST_ACCESS_KEY`, open Safari once to  
   `https://YOUR_HOST/mobile?access_key=YOUR_SECRET`  
   so the page stores the key; then Add to Home Screen from that URL or revisit after saving.

## Native iPhone app (SwiftUI)

See **`ios/SETUP.txt`**: real **Inbox Brief** app with **Summarise inbox** Siri phrase; talks to your **hosted** API only (not your Mac).

## 9) Run in the cloud (works when your Mac is off)

The app only needs a small always-on host plus **persistent disk** for `ms_token_cache.json` (Microsoft refresh tokens).

1. **Build** (example with Docker):
   ```bash
   docker build -t inbox-brief .
   docker run -p 8000:8000 -v inbox-data:/data --env-file .env inbox-brief
   ```
   Set `DATA_DIR=/data` (already the default in the Dockerfile) and mount a volume at `/data`.

2. **Platforms:** [Railway](https://railway.app), [Fly.io](https://fly.io), [Render](https://render.com), or any VPS. Add a **volume** mounted at `/data` (or set `DATA_DIR` to your mounted path).

3. **Environment:** Copy all variables from `.env` into the host’s env config. Set **`DIGEST_ACCESS_KEY`** to a long random string so `/digest` is not public.

4. **One-time Microsoft sign-in** after deploy (from your laptop):
   ```bash
   curl -X POST https://YOUR_HOST/auth/start
   # complete device login in browser
   curl -X POST https://YOUR_HOST/auth/complete
   ```
   Tokens are saved on the volume; the service can refresh them without your Mac.

5. **Phone / Siri:** Use `https://YOUR_HOST/digest/spoken?access_key=...` in Shortcuts and the mobile page as above.

### Render: keep Microsoft sign-in across deploys

Tokens live in **`ms_token_cache.json`** under **`DATA_DIR`**. The Docker image uses **`DATA_DIR=/data`**. If the container has **no persistent disk**, every deploy starts with an empty filesystem → you must run **`/auth/start`** and **`/auth/complete`** again.

**If you already have a Render Web Service** (e.g. `inbox-brief`):

1. Open the service in the [Render Dashboard](https://dashboard.render.com/).
2. Go to **Disks** (or **Persistence**).
3. **Add disk** with **Mount path** exactly **`/data`**. **1 GB** is enough.
4. Save (Render will redeploy).
5. **Sign in once more** with `auth/start` + `auth/complete` so the token file is written **on that disk**. Later deploys will reuse it.

Persistent disks may require a **paid** instance type on Render; if **Disks** is disabled on your plan, upgrade the service or use another host with a volume.

**Blueprint:** The repo root **`render.yaml`** defines the same **`/data`** disk for **New → Blueprint** setups (set all env vars in the dashboard or in the YAML as needed).

## Behavior Notes

- The service only fetches unread inbox messages.
- Each run summarizes **all** messages Graph reports as unread (not “only new since last time”).
- Ignore rules are applied before AI call to save tokens.
- It does **not** modify your mailbox: no mark-as-read, no archive, no delete, no move.
- It does **not** send replies/emails in this version.
- Graph permission scope is read-only (`Mail.Read`).

## Troubleshooting: `/digest` returns 401 but `/debug/graph-me` is 200

Your token can be valid for **profile** (`/me`) but still get **401 on mail** if Graph sees you as a **guest user** in the app’s tenant (UPN contains `#EXT#`, e.g. `user#EXT#@something.onmicrosoft.com`). That shadow account usually has **no Exchange mailbox** in that tenant, so `Mail.Read` calls fail.

**Fix:** Register the app in the **same Entra tenant as your real mailbox** (where your work account is a normal member, not a guest). Set `MS_TENANT_ID` to that tenant’s Directory ID and `MS_CLIENT_ID` to that registration’s client ID. Sign in again. Run `GET /debug/graph-account` to confirm `looks_like_guest_user_in_this_tenant` is false.

## Next Upgrades (optional)

- Add webhook subscriptions instead of polling.
- Auto-mark ignored newsletters as read.
- Add calendar/task extraction.
- Send push notifications for only `high` priority.
- Add web dashboard for rule editing.
