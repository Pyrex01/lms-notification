import os
import sys
import json
import re
import hashlib
import html as html_mod
from datetime import datetime
from pathlib import Path

import requests
from bs4 import BeautifulSoup
from Crypto.Cipher import AES
from Crypto.Util.Padding import pad
import base64

BASE_URL = "https://lms.lpuonline.com"
STATE_FILE = Path("/tmp/lms_last_seen.json")

AES_KEY = b'8080808080808080'
AES_IV = b'8080808080808080'

REG_NO = os.environ.get("REG_NO", "322100786")
PASSWORD = os.environ.get("PASSWORD", "izM-X4U!4.ZbpL2")
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")


def aes_encrypt(plaintext: str) -> str:
    cipher = AES.new(AES_KEY, AES.MODE_CBC, AES_IV)
    padded = pad(plaintext.encode("utf-8"), AES.block_size)
    return base64.b64encode(cipher.encrypt(padded)).decode("utf-8")


def login(session: requests.Session) -> bool:
    enc_user = aes_encrypt(REG_NO)
    enc_pass = aes_encrypt(PASSWORD)
    data = {
        "username": REG_NO,
        "password": PASSWORD,
        "HDUser": enc_user,
        "HDpass": enc_pass,
        "returnUrl": "",
        "source": "i",
    }
    resp = session.post(f"{BASE_URL}/", data=data, allow_redirects=True)
    ok = resp.status_code == 200 and resp.url == f"{BASE_URL}/Dashboard"
    if ok:
        print(f"[LOGIN] Success — session: {resp.url}")
    else:
        print(f"[LOGIN] Failed — status={resp.status_code}, url={resp.url}")
    return ok


def fetch_announcements(session: requests.Session) -> list[dict]:
    headers = {
        "X-Requested-With": "XMLHttpRequest",
        "Referer": f"{BASE_URL}/Dashboard",
    }
    resp = session.get(
        f"{BASE_URL}/api/webapi/AnnouncementDetails", headers=headers
    )
    if resp.status_code != 200:
        print(f"[ANNOUNCEMENTS] HTTP {resp.status_code}")
        return []
    data = resp.json()
    print(f"[ANNOUNCEMENTS] Fetched {len(data)} items")
    return data


def fetch_messages(session: requests.Session) -> str:
    headers = {
        "X-Requested-With": "XMLHttpRequest",
        "Referer": f"{BASE_URL}/Dashboard",
    }
    resp = session.post(
        f"{BASE_URL}/api/webapi/GetStudentMessages",
        headers=headers,
        json={},
    )
    if resp.status_code != 200:
        print(f"[MESSAGES] HTTP {resp.status_code}")
        return ""
    print(f"[MESSAGES] Fetched HTML ({len(resp.text)} bytes)")
    return resp.text


def parse_messages(html_str: str) -> list[dict]:
    items = []
    soup = BeautifulSoup(html_str, "html.parser")
    for div in soup.find_all("div", class_="mycoursesdiv"):
        subject_el = div.find("div", class_="font-weight-medium")
        body_el = div.find("p", class_="text-small")
        subject = subject_el.get_text(strip=True) if subject_el else ""
        body = body_el.get_text(strip=True) if body_el else ""
        items.append({"subject": subject, "body": body})
    return items


def strip_html(text: str) -> str:
    text = re.sub(r"<br\s*/?>", "\n", text)
    text = re.sub(r"<[^>]+>", "", text)
    text = html_mod.unescape(text)
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    return text


def make_announcement_id(ann: dict) -> str:
    aid = str(ann.get("announcementid", "") or "")
    if aid:
        return f"ann:{aid}"
    sub = (ann.get("subject") or "").strip()
    cat = (ann.get("Category") or "").strip()
    date = (ann.get("HeaderDate") or "").strip()
    raw = f"{sub}|{cat}|{date}"
    return f"ann:hash:{hashlib.md5(raw.encode()).hexdigest()[:12]}"


def make_message_id(msg: dict) -> str:
    raw = f"{msg['subject']}|{msg['body']}"
    return f"msg:{hashlib.md5(raw.encode()).hexdigest()[:16]}"


def load_state() -> set:
    if STATE_FILE.exists():
        data = json.loads(STATE_FILE.read_text())
        return set(data.get("seen_ids", []))
    return set()


def save_state(ids: set):
    STATE_FILE.write_text(json.dumps({"seen_ids": list(ids)}, indent=2))


def send_telegram(text: str) -> bool:
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("[TELEGRAM] Skipped — token or chat ID not configured")
        return False
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    try:
        resp = requests.post(url, json=payload, timeout=15)
        ok = resp.status_code == 200
        if ok:
            print(f"[TELEGRAM] Sent ({len(text)} chars)")
        else:
            print(f"[TELEGRAM] Error {resp.status_code}: {resp.text[:200]}")
        return ok
    except requests.RequestException as e:
        print(f"[TELEGRAM] Exception: {e}")
        return False


def send_telegram_bulk(items: list[str], header: str, item_type: str):
    if not items:
        print(f"[{item_type}] No new items to send")
        return

    chunks = []
    current = f"<b>{header}</b>\n\n"
    idx = 1
    for item in items:
        entry = f"<b>{idx}.</b> {item}\n\n"
        if len(current) + len(entry) > 3800:
            chunks.append(current)
            current = f"<b>{header} (cont.)</b>\n\n"
        current += entry
        idx += 1
    if current.strip():
        chunks.append(current)

    for chunk in chunks:
        if TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID:
            send_telegram(chunk)
        else:
            print(f"\n[{item_type}] Would send to Telegram:\n{chunk[:2000]}\n...")


def format_announcement(ann: dict) -> str:
    lines = []
    subject = ann.get("subject", "").strip()
    if subject:
        lines.append(f"{subject}")
    category = ann.get("Category", "")
    if category:
        lines.append(f"[{category}]")
    body = strip_html(ann.get("announcement", ""))
    if body:
        lines.append(f"{body[:400]}")
    return "\n".join(lines)


def format_message(msg: dict) -> str:
    lines = []
    lines.append(msg["subject"])
    body = msg["body"]
    if body:
        lines.append(body[:400])
    return "\n".join(lines)


def main():
    print("=" * 60)
    print(f"LPU LMS Scraper — {datetime.now().isoformat()}")
    print("=" * 60)

    session = requests.Session()
    session.headers.update({
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/120.0.0.0 Safari/537.36"
        )
    })

    if not login(session):
        sys.exit(1)

    seen = load_state()
    print(f"[STATE] Previously seen IDs: {len(seen)}")

    # --- Announcements ---
    announcements = fetch_announcements(session)
    new_announcements = []
    for ann in announcements:
        aid = make_announcement_id(ann)
        if aid not in seen:
            new_announcements.append(ann)
            seen.add(aid)

    print(f"\n[ANNOUNCEMENTS] New: {len(new_announcements)} / {len(announcements)}")
    for ann in new_announcements:
        print()
        print("-" * 60)
        print(f"  Subject  : {ann.get('subject', '')}")
        print(f"  Category : {ann.get('Category', '')}")
        print(f"  Date     : {ann.get('HeaderDate', '')}")
        print(f"  Posted by: {ann.get('uploadedby', '')}")
        print(f"  Body     :")
        print(f"    {strip_html(ann.get('announcement', ''))[:500]}")

    # --- Messages ---
    messages_html = fetch_messages(session)
    messages = parse_messages(messages_html)
    new_messages = []
    for msg in messages:
        mid = make_message_id(msg)
        if mid not in seen:
            new_messages.append(msg)
            seen.add(mid)

    print(f"\n[MESSAGES] New: {len(new_messages)} / {len(messages)}")
    for i, msg in enumerate(new_messages, 1):
        print()
        print("-" * 60)
        print(f"  Message #{i}")
        print(f"  Subject: {msg['subject']}")
        print(f"  Body   : {msg['body'][:300]}")

    # --- Telegram ---
    if new_announcements:
        formatted = [format_announcement(a) for a in new_announcements]
        send_telegram_bulk(formatted, "New Announcements", "ANNOUNCEMENTS")

    if new_messages:
        formatted = [format_message(m) for m in new_messages]
        send_telegram_bulk(formatted, "New Messages", "MESSAGES")

    if not new_announcements and not new_messages:
        print("\n[INFO] No new announcements or messages.")
        if TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID:
            send_telegram("<b>LPU LMS Check</b>\n\nNo new announcements or messages.")

    # Save state
    save_state(seen)
    print(f"\n[STATE] Saved {len(seen)} IDs to {STATE_FILE}")
    print("\n[DONE]")


if __name__ == "__main__":
    main()
