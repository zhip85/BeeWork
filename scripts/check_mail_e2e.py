"""T-008 end-to-end: platform tenant invitation → real SMTP (Mailpit) → accept the mailed link.

Runs inside the api container against the running stack:
    docker compose exec api python /app/scripts/check_mail_e2e.py

Creates one throwaway tenant and admin account; Mailpit is the local SMTP catcher
(Web UI http://127.0.0.1:8025). No third-party provider or real mailbox is involved.
"""
import os
import re
import sys
import time
import uuid

import httpx

API = os.environ.get("BEEWORK_API", "http://localhost:8000")
MAILPIT = os.environ.get("MAILPIT_URL", "http://mailpit:8025")
ORIGIN = os.environ.get("APP_ORIGIN", "http://localhost:5173")
PLATFORM_PASSWORD = os.environ.get("BOOTSTRAP_PLATFORM_ADMIN_PASSWORD", "BeeWork-Platform-2026!")
SETTLE_SECONDS = 60
SUFFIX = uuid.uuid4().hex[:8]
ADMIN_EMAIL = f"mail-e2e-{SUFFIX}@beework.dev"
SLUG = f"mail-e2e-{SUFFIX}"
PASSWORD = "MailE2e-Password-2026!"


def check(label: str, condition: bool, detail: object = "") -> None:
    print(f"[{'OK' if condition else 'FAIL'}] {label}{f' — {detail}' if detail else ''}")
    if not condition:
        sys.exit(1)


def wait_for(label: str, read, done) -> object:
    deadline = time.time() + SETTLE_SECONDS
    value = read()
    while not done(value) and time.time() < deadline:
        time.sleep(1.5)
        value = read()
    check(label, done(value), str(value)[:300])
    return value


client = httpx.Client(base_url=API, timeout=30, headers={"Origin": ORIGIN})
login = client.post(
    "/api/v1/auth/login",
    json={"email": "platform@beework.dev", "password": PLATFORM_PASSWORD},
)
check("platform admin login", login.status_code == 200, login.text[:200])
client.headers["X-CSRF-Token"] = client.cookies["beework_csrf"]

created = client.post(
    "/api/v1/platform/tenants",
    json={
        "name": f"Mail E2E {SUFFIX}", "slug": SLUG, "seat_limit": 5,
        "admin_name": "Mail E2E Admin", "admin_email": ADMIN_EMAIL,
        "send_admin_invitation": True,
    },
)
check("tenant created in invitation mode", created.status_code == 201, created.text[:300])
tenant_id = created.json()["id"]


def find_message():
    response = httpx.get(f"{MAILPIT}/api/v1/messages", timeout=10)
    response.raise_for_status()
    for item in response.json()["messages"]:
        if any(ADMIN_EMAIL in str(address) for address in item.get("To", [])):
            detail = httpx.get(f"{MAILPIT}/api/v1/message/{item['ID']}", timeout=10)
            detail.raise_for_status()
            return detail.json()
    return None


message = wait_for("Mailpit received the invitation mail", find_message, bool)
check("mail subject mentions the workspace", "Mail E2E" in message.get("Subject", ""), message.get("Subject"))
body = (message.get("Text") or "") + (message.get("HTML") or "")
check("mail body contains no plaintext password", PASSWORD not in body)
match = re.search(r"(/invite\?token=[A-Za-z0-9_\-]+)", body)
check("mail body contains the invitation link", match is not None)
token = match.group(1).split("token=", 1)[1]

deliveries = client.get("/api/v1/platform/mail/deliveries?status=sent&limit=100")
check("delivery record visible on the platform", deliveries.status_code == 200, deliveries.text[:200])
records = [item for item in deliveries.json()["items"] if item["template"] == "tenant_admin_invitation"]
check("delivery is marked sent with a masked recipient", bool(records) and records[0]["recipient_masked"].startswith("m***@"), records[:1])

accepted = client.post(
    "/api/v1/members/invitations/accept",
    json={"token": token, "password": PASSWORD, "display_name": "Mail E2E Admin"},
)
check("invitation link can be accepted", accepted.status_code == 200, accepted.text[:300])
check("new admin belongs to the tenant", any(
    m["id"] == tenant_id for m in accepted.json()["memberships"]
), accepted.json().get("memberships"))

signed_in = httpx.post(
    f"{API}/api/v1/auth/login", json={"email": ADMIN_EMAIL, "password": PASSWORD},
    headers={"Origin": ORIGIN}, timeout=30,
)
check("the admin can sign in with the password set from the mail", signed_in.status_code == 200, signed_in.text[:200])
print("T-008 mail e2e passed (real SMTP via Mailpit).")
