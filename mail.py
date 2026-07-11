import json
import os
import urllib.error
import urllib.request
from datetime import datetime

from checker import TZ
from config import token_url

RESEND_URL = "https://api.resend.com/emails"
DAILY_LIMIT = int(os.getenv("DAILY_MAIL_LIMIT", "90"))


def _send_raw(to, subject, text_body):
    api_key = os.getenv("RESEND_API_KEY")
    if not api_key:
        print(f"  Mail hoppas över (ingen RESEND_API_KEY): {to}")
        return None
    from_addr = os.getenv("FROM_EMAIL", "noreply@varfinnsdet.se")
    payload = json.dumps({
        "from": from_addr,
        "to": [to],
        "subject": subject,
        "text": text_body,
    }).encode()
    req = urllib.request.Request(
        RESEND_URL,
        data=payload,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "User-Agent": "Mozilla/5.0 (compatible; varfinnsdet/1.0)",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            result = json.loads(resp.read())
        print(f"  Mail: {to} — {subject[:60]}")
        return result.get("id")
    except urllib.error.HTTPError as e:
        err_body = e.read().decode(errors="replace")
        raise RuntimeError(f"Resend {e.code}: {err_body}") from e


def _within_daily_limit():
    from db import get_db
    today = datetime.now(TZ).strftime("%Y-%m-%d")
    with get_db() as db:
        # Atomic check-and-increment in a single statement -- a separate
        # SELECT-then-UPDATE lets two threads (the poll loop + a request
        # thread, under gunicorn --threads 4) both read the same count,
        # both pass the < DAILY_LIMIT check, and both write, letting the
        # daily cap be exceeded.
        row = db.execute(
            "INSERT INTO daily_mail_count (date, count) VALUES (?, 1) "
            "ON CONFLICT(date) DO UPDATE SET count = count + 1 WHERE count < ? "
            "RETURNING count",
            [today, DAILY_LIMIT],
        ).fetchone()
        db.commit()
    if row is None:
        print(f"  Daglig mailgräns nådd ({DAILY_LIMIT}/{DAILY_LIMIT})")
        return False
    return True


def _domain(site_url):
    return (site_url or "varfinnsdet.se").replace("https://", "").replace("http://", "").rstrip("/")


def send_confirmation(to, token, site_url, medication_name=None):
    if not _within_daily_limit():
        return False
    domain = _domain(site_url)
    confirm_url = token_url(site_url, "confirm", token)
    intro = (
        f"Du har anmält en bevakning för {medication_name}.\n\n"
        if medication_name else
        "Du har anmält en bevakning.\n\n"
    )
    body = (
        intro
        + f"Bekräfta din e-postadress inom 48 timmar:\n{confirm_url}\n\n"
        + "Om du inte gjort detta kan du ignorera detta e-postmeddelande.\n"
    )
    return bool(_send_raw(to, f"Bekräfta din prenumeration på {domain}", body))


def send_notification(to, medication_name, pharmacies, unsubscribe_token, manage_token, expires_at, site_url, medication_url=None, checked_at=None):
    if not _within_daily_limit():
        return False
    lines = [f"• {ph['name']}" for ph in pharmacies[:20]]
    if len(pharmacies) > 20:
        lines.append(f"... och {len(pharmacies) - 20} fler apotek")
    # Reuse the actual poll's timestamp when the caller has it -- generating
    # a fresh datetime.now() per recipient means a popular medication with
    # many subscribers shows a slightly different "Kontrollerat" time in
    # each email, even though they're all reporting the same poll result.
    checked_at = checked_at or datetime.now(TZ).strftime("%Y-%m-%d %H:%M")
    body = (
        f"{medication_name} finns nu i lager på {len(pharmacies)} apotek i Sverige.\n\n"
        + (f"Se lagerstatus och alla apotek: {medication_url}\n\n" if medication_url else "")
        + "\n".join(lines)
        + "\n\nRing apoteket innan du åker — lagret kan förändras snabbt.\n\n"
        f"Kontrollerat: {checked_at}\n\n"
        f"Hantera dina bevakningar: {token_url(site_url, 'manage', manage_token)}\n"
        f"Avregistrera mig: {token_url(site_url, 'unsubscribe', unsubscribe_token)}\n"
        f"Bevakningen löper ut automatiskt {expires_at[:10]}.\n"
    )
    return bool(_send_raw(to, "Ditt bevakade läkemedel finns nu i lager", body))


def send_renewal_reminder(to, expires_at, extend_token, manage_token, site_url):
    if not _within_daily_limit():
        return False
    body = (
        f"Din bevakning på {_domain(site_url)} löper ut {expires_at[:10]}.\n\n"
        f"Förläng 30 dagar till med ett klick:\n{token_url(site_url, 'extend', extend_token)}\n\n"
        "Klickar du inte avslutas bevakningen automatiskt.\n\n"
        f"Hantera dina bevakningar: {token_url(site_url, 'manage', manage_token)}\n"
    )
    return bool(_send_raw(to, "Din bevakning löper ut om 5 dagar", body))
