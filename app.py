from __future__ import annotations

import base64
import json
import os
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx
import msal
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from openai import OpenAI
from starlette.responses import HTMLResponse, PlainTextResponse

load_dotenv()

BASE_DIR = Path(__file__).parent
_DATA_DIR = Path(os.getenv("DATA_DIR", str(BASE_DIR))).resolve()
TOKEN_CACHE_PATH = _DATA_DIR / "ms_token_cache.json"
DEVICE_FLOW_PATH = _DATA_DIR / "ms_device_flow.json"

# Safety-first scope: read-only mailbox access. No write/archive/send permissions.
# MSAL adds reserved scopes internally for device flow.
GRAPH_SCOPE = ["Mail.Read", "User.Read"]
# $filter on messages requires ConsistencyLevel: eventual + $count=true (Graph docs).
GRAPH_MESSAGES_FILTERED = (
    "https://graph.microsoft.com/v1.0/me/mailFolders/inbox/messages"
    "?$select=id,subject,bodyPreview,receivedDateTime,from,webLink,isRead"
    "&$orderby=receivedDateTime desc"
    "&$filter=isRead eq false"
    "&$count=true"
)
# Fallback: no filter (avoids advanced-query requirements); we filter unread in code.
GRAPH_MESSAGES_INBOX = (
    "https://graph.microsoft.com/v1.0/me/mailFolders/inbox/messages"
    "?$select=id,subject,bodyPreview,receivedDateTime,from,webLink,isRead"
    "&$orderby=receivedDateTime desc"
)
GRAPH_ME_URL = "https://graph.microsoft.com/v1.0/me"
GRAPH_MAIL_PROBE_URL = "https://graph.microsoft.com/v1.0/me/messages?$top=1&$select=id,subject"


@dataclass
class Settings:
    app_host: str
    app_port: int
    ms_client_id: str
    ms_tenant_id: str
    openai_api_key: str
    openai_model: str
    max_emails_per_run: int
    ignore_senders: set[str]
    ignore_domains: set[str]
    vip_senders: set[str]
    digest_access_key: str


def _split_csv(value: str | None) -> set[str]:
    if not value:
        return set()
    return {x.strip().lower() for x in value.split(",") if x.strip()}


def load_settings() -> Settings:
    ms_client_id = os.getenv("MS_CLIENT_ID", "").strip()
    if (not ms_client_id) or ms_client_id == "your-client-id":
        raise RuntimeError("Set MS_CLIENT_ID in .env to your real Azure app client ID.")

    openai_api_key = os.getenv("OPENAI_API_KEY", "").strip()
    if (not openai_api_key) or openai_api_key == "your-openai-api-key":
        raise RuntimeError("Set OPENAI_API_KEY in .env to a real OpenAI API key.")

    tenant_id = os.getenv("MS_TENANT_ID", "common").strip() or "common"

    return Settings(
        app_host=os.getenv("APP_HOST", "127.0.0.1").strip(),
        app_port=int(os.getenv("APP_PORT", "8000").strip()),
        ms_client_id=ms_client_id,
        ms_tenant_id=tenant_id,
        openai_api_key=openai_api_key,
        openai_model=os.getenv("OPENAI_MODEL", "gpt-4o-mini").strip(),
        max_emails_per_run=int(os.getenv("MAX_EMAILS_PER_RUN", "25").strip()),
        ignore_senders=_split_csv(os.getenv("IGNORE_SENDERS")),
        ignore_domains=_split_csv(os.getenv("IGNORE_DOMAINS")),
        vip_senders=_split_csv(os.getenv("VIP_SENDERS")),
        digest_access_key=os.getenv("DIGEST_ACCESS_KEY", "").strip(),
    )


def get_msal_app(settings: Settings) -> msal.PublicClientApplication:
    authority = f"https://login.microsoftonline.com/{settings.ms_tenant_id}"
    cache = msal.SerializableTokenCache()
    if TOKEN_CACHE_PATH.exists():
        cache.deserialize(TOKEN_CACHE_PATH.read_text())

    app = msal.PublicClientApplication(
        client_id=settings.ms_client_id,
        authority=authority,
        token_cache=cache,
    )
    return app


def persist_cache(app: msal.PublicClientApplication) -> None:
    cache = app.token_cache
    if cache.has_state_changed:
        TOKEN_CACHE_PATH.write_text(cache.serialize())


def _jwt_payload_unverified(access_token: str) -> dict[str, Any]:
    try:
        parts = access_token.split(".")
        if len(parts) != 3:
            return {}
        payload = parts[1] + "=" * (-len(parts[1]) % 4)
        raw = base64.urlsafe_b64decode(payload.encode("ascii"))
        return json.loads(raw.decode("utf-8"))
    except Exception:
        return {}


def get_access_token(
    settings: Settings, force_refresh: bool = False
) -> tuple[str | None, dict[str, Any]]:
    app = get_msal_app(settings)
    accounts = app.get_accounts()
    if not accounts:
        return None, {}

    token_result = app.acquire_token_silent(
        scopes=GRAPH_SCOPE,
        account=accounts[0],
        force_refresh=force_refresh,
    )
    persist_cache(app)
    if token_result and "access_token" in token_result:
        return token_result["access_token"], token_result
    return None, token_result or {}


def auth_start_device_code(settings: Settings) -> dict[str, Any]:
    app = get_msal_app(settings)
    flow = app.initiate_device_flow(scopes=GRAPH_SCOPE)
    if "user_code" not in flow:
        raise RuntimeError(f"Could not create device flow: {json.dumps(flow, indent=2)}")

    DEVICE_FLOW_PATH.write_text(json.dumps(flow))
    return {
        "message": flow.get("message", "Use device code flow to sign in."),
        "user_code": flow.get("user_code"),
        "verification_uri": flow.get("verification_uri"),
        "expires_in": flow.get("expires_in"),
    }


def auth_complete_device_code(settings: Settings) -> dict[str, Any]:
    if not DEVICE_FLOW_PATH.exists():
        raise RuntimeError("No active device flow found. Call POST /auth/start first.")

    flow = json.loads(DEVICE_FLOW_PATH.read_text())
    app = get_msal_app(settings)
    token_result = app.acquire_token_by_device_flow(flow)
    persist_cache(app)
    if "access_token" not in token_result:
        raise RuntimeError(f"Sign-in failed: {token_result.get('error_description', token_result)}")
    DEVICE_FLOW_PATH.unlink(missing_ok=True)

    return {
        "message": "Sign-in successful.",
        "account": token_result.get("id_token_claims", {}).get("preferred_username"),
    }


def clean_text(text: str) -> str:
    text = re.sub(r"<[^>]+>", " ", text or "")
    text = re.sub(r"\s+", " ", text).strip()
    return text


def sender_email(message: dict[str, Any]) -> str:
    return (
        message.get("from", {})
        .get("emailAddress", {})
        .get("address", "")
        .strip()
        .lower()
    )


def should_ignore(sender: str, settings: Settings) -> bool:
    if sender in settings.ignore_senders:
        return True
    domain = sender.split("@")[-1] if "@" in sender else ""
    return bool(domain and domain in settings.ignore_domains)


def _check_digest_access(access_key: str | None, settings: Settings) -> None:
    if not settings.digest_access_key:
        return
    if (access_key or "").strip() != settings.digest_access_key:
        raise HTTPException(
            status_code=401,
            detail="Missing or invalid access_key. Pass ?access_key=... on digest URLs.",
        )


def _normalize_ai_row(row: dict[str, Any]) -> dict[str, str]:
    action = str(row.get("action", "respond")).lower()
    priority = str(row.get("priority", "medium")).lower()
    if action not in {"respond", "ignore", "confirmation"}:
        action = "respond"
    if priority not in {"high", "medium", "low"}:
        priority = "medium"
    summary = str(row.get("summary", "")).strip()
    if len(summary) > 600:
        summary = summary[:597] + "..."
    return {
        "summary": summary or "No summary.",
        "action": action,
        "priority": priority,
        "reason": str(row.get("reason", ""))[:200],
    }


def classify_emails_batch(
    client: OpenAI,
    settings: Settings,
    emails: list[tuple[str, dict[str, Any]]],
) -> dict[str, dict[str, str]]:
    """One OpenAI call for many messages — faster than per-email calls. Returns map id -> fields."""
    if not emails:
        return {}

    payload = []
    for msg_id, email in emails:
        sender = sender_email(email)
        payload.append(
            {
                "id": msg_id,
                "sender": sender,
                "vip": sender in settings.vip_senders,
                "subject": clean_text(email.get("subject", ""))[:220],
                "preview": clean_text(email.get("bodyPreview", ""))[:450],
            }
        )

    system = (
        "You triage work email. For EACH item you MUST write an original summary in your own words — "
        "never copy or paste the preview text; paraphrase what the sender wants.\n"
        "Rules per item:\n"
        "- summary: max 2 short sentences OR 240 characters, whichever is shorter. Plain language.\n"
        "- action: exactly one of: respond, ignore, confirmation.\n"
        "- priority: exactly one of: high, medium, low.\n"
        "- reason: max 12 words.\n"
        "promotional/newsletter → ignore; explicit ask/deadline/money/legal/customer/manager → respond; "
        "receipt/order/code/verification → confirmation. vip true → bias priority up.\n"
        "Return JSON with top-level key \"items\": array of objects with keys id, summary, action, priority, reason. "
        "Include every input id exactly once."
    )
    user = "Emails as JSON:\n" + json.dumps(payload, ensure_ascii=False)

    try:
        completion = client.chat.completions.create(
            model=settings.openai_model,
            temperature=0.2,
            response_format={"type": "json_object"},
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
        )
        raw = (completion.choices[0].message.content or "").strip()
        data = json.loads(raw)
    except Exception:
        return {}

    items = data.get("items")
    if not isinstance(items, list):
        return {}

    out: dict[str, dict[str, str]] = {}
    for row in items:
        if not isinstance(row, dict):
            continue
        mid = row.get("id")
        if mid is None:
            continue
        out[str(mid)] = _normalize_ai_row(row)
    return out


def _ai_results_for_emails(
    client: OpenAI, settings: Settings, email_tuples: list[tuple[str, dict[str, Any]]]
) -> dict[str, dict[str, str]]:
    """Chunked batch calls (faster than N singles); retry misses; placeholder avoids dumping raw body."""
    if not email_tuples:
        return {}
    by_id: dict[str, dict[str, str]] = {}
    chunk_size = int(os.getenv("AI_BATCH_CHUNK", "15").strip() or "15")
    chunk_size = max(5, min(chunk_size, 30))

    for start in range(0, len(email_tuples), chunk_size):
        batch = email_tuples[start : start + chunk_size]
        by_id.update(classify_emails_batch(client, settings, batch))
        missing = [(mid, em) for mid, em in batch if mid not in by_id]
        if missing:
            by_id.update(classify_emails_batch(client, settings, missing))

    for mid, em in email_tuples:
        if mid in by_id:
            continue
        subj = clean_text(em.get("subject", ""))[:120]
        snd = sender_email(em) or "unknown"
        by_id[mid] = {
            "summary": f"Unread from {snd}: {subj or 'no subject'}."[:240],
            "action": "respond",
            "priority": "medium",
            "reason": "AI batch did not return this id",
        }
    return by_id


def _graph_headers(access_token: str, *, eventual: bool) -> dict[str, str]:
    token = (access_token or "").strip()
    h: dict[str, str] = {"Authorization": f"Bearer {token}"}
    if eventual:
        h["ConsistencyLevel"] = "eventual"
    return h


async def fetch_unread_emails(access_token: str, limit: int) -> tuple[int, str, list[dict[str, Any]]]:
    top = max(1, limit)
    async with httpx.AsyncClient(timeout=30.0) as client:
        # 1) Preferred: filtered unread (needs eventual + $count)
        r1 = await client.get(
            GRAPH_MESSAGES_FILTERED + f"&$top={top}",
            headers=_graph_headers(access_token, eventual=True),
        )
        if r1.status_code == 200:
            data = r1.json()
            return r1.status_code, r1.text or "", data.get("value", [])

        # 2) Fallback: pull recent inbox, filter unread locally
        r2 = await client.get(
            GRAPH_MESSAGES_INBOX + f"&$top={top * 3}",
            headers=_graph_headers(access_token, eventual=False),
        )
        body = r2.text or ""
        if r2.status_code >= 400:
            return r2.status_code, body, []
        data = r2.json()
        rows = [m for m in data.get("value", []) if not m.get("isRead", False)]
        return r2.status_code, body, rows[:top]


def priority_rank(value: str) -> int:
    return {"high": 0, "medium": 1, "low": 2}.get(value, 1)


def build_spoken_digest(items: list[dict[str, Any]]) -> str:
    if not items:
        return "No unread emails need attention right now."

    top = items[:8]
    lines = [f"You have {len(items)} unread emails after filtering."]
    for idx, item in enumerate(top, start=1):
        lines.append(
            f"{idx}. {item['priority'].upper()} - {item['sender']}. {item['summary']} "
            f"Action: {item['action']}."
        )
    return " ".join(lines)


settings = load_settings()
openai_client = OpenAI(api_key=settings.openai_api_key)
app = FastAPI(title="Outlook AI Briefing")


@app.on_event("startup")
def _ensure_data_dir() -> None:
    _DATA_DIR.mkdir(parents=True, exist_ok=True)

# Add to Home Screen on iPhone: open http://<mac-ip>:8000/mobile → Share → Add to Home Screen.
MOBILE_APP_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover" />
  <meta name="apple-mobile-web-app-capable" content="yes" />
  <meta name="apple-mobile-web-app-status-bar-style" content="black-translucent" />
  <meta name="apple-mobile-web-app-title" content="Inbox Brief" />
  <meta name="theme-color" content="#0b0f14" />
  <title>Inbox Brief</title>
  <style>
    :root { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; }
    * { box-sizing: border-box; }
    body {
      margin: 0; min-height: 100dvh; background: #0b0f14; color: #e7ecf3;
      padding: max(16px, env(safe-area-inset-top)) 20px max(24px, env(safe-area-inset-bottom));
      display: flex; flex-direction: column; align-items: stretch;
    }
    h1 { font-size: 1.35rem; font-weight: 600; margin: 0 0 6px; letter-spacing: -0.02em; }
    p.sub { margin: 0 0 20px; font-size: 0.85rem; color: #8b98a8; }
    button.primary {
      border: none; border-radius: 14px; padding: 16px 20px; font-size: 1.05rem; font-weight: 600;
      background: linear-gradient(145deg, #3d8bfd, #2563eb); color: #fff; cursor: pointer;
      box-shadow: 0 4px 20px rgba(37, 99, 235, 0.35);
    }
    button.primary:disabled { opacity: 0.55; cursor: wait; }
    button.secondary {
      margin-top: 12px; border: 1px solid #2a3544; border-radius: 14px; padding: 14px 18px;
      font-size: 0.95rem; background: transparent; color: #9fb0c3; cursor: pointer;
    }
    #out {
      margin-top: 22px; padding: 16px; border-radius: 14px; background: #121922; border: 1px solid #1e2a3a;
      font-size: 0.95rem; line-height: 1.5; white-space: pre-wrap; min-height: 4em; color: #dce4ee;
    }
    .err { color: #f87171; border-color: #3f1d1d; background: #1a1010; }
    .hint { font-size: 0.75rem; color: #5c6b7e; margin-top: auto; padding-top: 28px; line-height: 1.4; }
  </style>
</head>
<body>
  <h1>Inbox Brief</h1>
  <p class="sub">Unread mail, AI-summarized from your inbox.</p>
  <button type="button" class="primary" id="go">Brief my inbox</button>
  <button type="button" class="secondary" id="speak" disabled>Read aloud</button>
  <div id="out" aria-live="polite"></div>
  <p class="hint">Tip: Safari → Share → Add to Home Screen. If you set DIGEST_ACCESS_KEY on the server, open this page once with <code>?access_key=YOUR_SECRET</code> so it can save the key.<br />
  Host the app on a small cloud service so it works when your computer is off.</p>
  <script>
    (function() {
      const p = new URLSearchParams(location.search);
      const k = p.get("access_key");
      if (k) localStorage.setItem("inbox_access_key", k);
    })();
    function digestQuery() {
      const k = localStorage.getItem("inbox_access_key");
      return k ? ("?access_key=" + encodeURIComponent(k)) : "";
    }
    const out = document.getElementById("out");
    const go = document.getElementById("go");
    const speakBtn = document.getElementById("speak");
    let lastText = "";
    function setErr(msg) {
      out.textContent = msg;
      out.classList.add("err");
      speakBtn.disabled = true;
    }
    go.addEventListener("click", async () => {
      out.classList.remove("err");
      out.textContent = "Loading…";
      go.disabled = true;
      speakBtn.disabled = true;
      try {
        const r = await fetch("/digest/spoken" + digestQuery(), { cache: "no-store" });
        const t = await r.text();
        if (!r.ok) {
          setErr(t || ("Error " + r.status));
          return;
        }
        lastText = t;
        out.textContent = t;
        speakBtn.disabled = !t.trim();
      } catch (e) {
        setErr("Could not reach the inbox API. If this shortcut used your Mac’s address, it only works on the same Wi‑Fi. Use a hosted URL or a tunnel (ngrok, etc.); see README.");
      } finally {
        go.disabled = false;
      }
    });
    speakBtn.addEventListener("click", () => {
      if (!lastText.trim()) return;
      window.speechSynthesis.cancel();
      const u = new SpeechSynthesisUtterance(lastText);
      u.rate = 1;
      window.speechSynthesis.speak(u);
    });
  </script>
</body>
</html>"""


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/mobile", response_class=HTMLResponse)
def mobile_app_shell() -> HTMLResponse:
    return HTMLResponse(content=MOBILE_APP_HTML)


@app.get("/policy")
def policy() -> dict[str, Any]:
    return {
        "mailbox_mutation": "disabled",
        "replying": "disabled",
        "delete_archive_move": "disabled",
        "mark_read_unread": "disabled",
        "graph_scopes": GRAPH_SCOPE,
        "note": "Service is read-only for mailbox content in this version.",
    }


@app.get("/debug/token")
def debug_token() -> dict[str, Any]:
    access_token, token_result = get_access_token(settings)
    if not access_token:
        raise HTTPException(
            status_code=401,
            detail="Not authenticated. Run auth flow first.",
        )
    claims = _jwt_payload_unverified(access_token)
    scp = claims.get("scp") or claims.get("roles") or ""
    return {
        "token_audience": claims.get("aud"),
        "token_tenant": claims.get("tid"),
        "token_app_id": claims.get("appid"),
        "scopes_in_token": scp,
        "expires_unix": claims.get("exp"),
        "msal_error": token_result.get("error"),
        "msal_error_description": token_result.get("error_description"),
        "hint": "If scopes_in_token lacks Mail.Read, delete ms_token_cache.json and sign in again.",
    }


@app.get("/debug/graph-me")
async def debug_graph_me() -> dict[str, Any]:
    access_token, _ = get_access_token(settings)
    if not access_token:
        raise HTTPException(status_code=401, detail="Not authenticated.")
    async with httpx.AsyncClient(timeout=20.0) as client:
        r = await client.get(
            GRAPH_ME_URL,
            headers=_graph_headers(access_token, eventual=False),
        )
    return {
        "status": r.status_code,
        "body_preview": (r.text or "")[:500],
    }


@app.get("/debug/graph-account")
async def debug_graph_account() -> dict[str, Any]:
    access_token, _ = get_access_token(settings)
    if not access_token:
        raise HTTPException(status_code=401, detail="Not authenticated.")
    async with httpx.AsyncClient(timeout=25.0) as client:
        r_me = await client.get(
            GRAPH_ME_URL,
            headers=_graph_headers(access_token, eventual=False),
        )
        me: dict[str, Any] = {}
        if r_me.status_code == 200:
            me = r_me.json()
        upn = str(me.get("userPrincipalName") or "")
        guest = "#EXT#" in upn.upper()

        r_mail = await client.get(
            GRAPH_MAIL_PROBE_URL,
            headers=_graph_headers(access_token, eventual=False),
        )
    return {
        "me_http_status": r_me.status_code,
        "user_principal_name": upn,
        "mail": me.get("mail"),
        "looks_like_guest_user_in_this_tenant": guest,
        "mail_probe_http_status": r_mail.status_code,
        "mail_probe_body_preview": (r_mail.text or "")[:600],
        "what_to_do_if_mail_401_and_guest_true": (
            "Register the app under your organization's Entra tenant (where gf@arieteandco.com is a "
            "normal member user with an Exchange mailbox). Put that tenant's Directory ID in "
            "MS_TENANT_ID and that app's Application (client) ID in MS_CLIENT_ID. Grant Mail.Read, "
            "sign in again with your work account. Your /me UPN should not contain #EXT#."
        ),
    }


@app.post("/auth/start")
def auth_start() -> dict[str, Any]:
    try:
        return auth_start_device_code(settings)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@app.post("/auth/complete")
def auth_complete() -> dict[str, str]:
    try:
        return auth_complete_device_code(settings)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@app.get("/digest")
async def digest(access_key: str | None = Query(None)) -> dict[str, Any]:
    _check_digest_access(access_key, settings)
    access_token, _ = get_access_token(settings)
    if not access_token:
        raise HTTPException(
            status_code=401,
            detail="Not authenticated. Run POST /auth/start, complete sign-in, then POST /auth/complete.",
        )

    status, body, unread = await fetch_unread_emails(access_token, settings.max_emails_per_run)
    if status == 401:
        access_token, tr = get_access_token(settings, force_refresh=True)
        if not access_token:
            raise HTTPException(
                status_code=502,
                detail=f"Graph returned 401; could not refresh token: {tr}",
            )
        status, body, unread = await fetch_unread_emails(access_token, settings.max_emails_per_run)
    if status >= 400:
        hint = ""
        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                r_acc = await client.get(
                    GRAPH_ME_URL,
                    headers=_graph_headers(access_token, eventual=False),
                )
            if r_acc.status_code == 200:
                me = r_acc.json()
                upn = str(me.get("userPrincipalName") or "")
                if "#EXT#" in upn.upper():
                    hint = (
                        " Your /me profile looks like a guest user (UPN contains #EXT#). "
                        "Mail.Read often fails because that shadow account has no mailbox in this tenant. "
                        "Fix: register the app in the Entra tenant where your real work mailbox lives "
                        "(e.g. Ariete & Co), set MS_CLIENT_ID and MS_TENANT_ID to that app, sign in again. "
                        "See GET /debug/graph-account."
                    )
        except Exception:
            pass
        raise HTTPException(
            status_code=502,
            detail=f"Graph API error: {status} {body}{hint}",
        )
    # Summarize every message Graph returns as unread — do not skip "already seen" IDs.
    # (Old behavior used state.db so each message only appeared once; that hid still-unread mail.)
    pending: list[tuple[str, dict[str, Any]]] = []
    for email in unread:
        msg_id = email.get("id")
        if not msg_id:
            continue
        sender = sender_email(email)
        if should_ignore(sender, settings):
            continue
        pending.append((str(msg_id), email))

    ai_by_id = _ai_results_for_emails(openai_client, settings, pending)

    results: list[dict[str, Any]] = []
    for msg_id, email in pending:
        sender = sender_email(email)
        ai = ai_by_id.get(msg_id, {})
        results.append(
            {
                "id": msg_id,
                "sender": sender or "unknown sender",
                "subject": clean_text(email.get("subject", "")),
                "received": email.get("receivedDateTime"),
                "action": ai.get("action", "respond"),
                "priority": ai.get("priority", "medium"),
                "reason": ai.get("reason", ""),
                "summary": ai.get("summary", "No summary."),
                "web_link": email.get("webLink"),
            }
        )

    results.sort(key=lambda x: (priority_rank(x["priority"]), x["received"] or ""))
    spoken = build_spoken_digest(results)

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "count": len(results),
        "items": results,
        "spoken_digest": spoken,
    }


@app.get("/digest/spoken", response_class=PlainTextResponse)
async def digest_spoken(access_key: str | None = Query(None)) -> PlainTextResponse:
    data = await digest(access_key=access_key)
    return PlainTextResponse(content=data["spoken_digest"])


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("app:app", host=settings.app_host, port=settings.app_port, reload=True)
