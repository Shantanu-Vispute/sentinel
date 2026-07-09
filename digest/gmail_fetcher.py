from config import MAX_FETCH_RESULTS
import html
import json
import base64
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from dataclasses import dataclass

STATE_DIR = Path(__file__).resolve().parent.parent / "state"
LAST_FETCH_PATH = STATE_DIR / "last_fetch.json"

@dataclass
class Newsletter:
    id: str
    subject: str
    sender: str
    date: str
    body: str
    snippet: str
    gmail_url: str = ""
    links: list[dict] = None
    images: list[dict] = None

    def __post_init__(self):
        if not self.gmail_url and self.id:
            self.gmail_url = f"https://mail.google.com/mail/u/0/#inbox/{
                self.id}"

def fetch_newsletters(
        service,
        since: datetime | None = None,
        max_results: int | None = None) -> list[Newsletter]:
    if since is None:
        since = _load_last_fetch() or (datetime.now(timezone.utc) - timedelta(days=7))

        if max_results is None:
            max_results = MAX_FETCH_RESULTS

    query = _build_newsletter_query(since)
    print(f"  Query: {query[:80]}...")
    print(f"  Fetching since: {since.strftime('%Y-%m-%d %H:%M')}")
    if max_results is None:
        print(f"  Limit: none (fetching all pages)")
    else:
        print(f"  Limit: {max_results}")

    message_ids = []
    page_token = None
    while True:
        if max_results is not None and len(message_ids) >= max_results:
            break
        page_size = 500 if max_results is None else min(
            max_results - len(message_ids), 500)
        results = (
            service.users()
            .messages()
            .list(
                userId="me",
                q=query,
                maxResults=page_size,
                pageToken=page_token,
            )
            .execute()
        )

        messages = results.get("messages", [])
        if not messages:
            break

        message_ids.extend(msg["id"] for msg in messages)
        page_token = results.get("nextPageToken")
        if not page_token:
            break

    print(f"  Found {len(message_ids)} newsletter emails")

    newsletters = []
    skipped_counts = {"non_newsletter": 0, "too_short": 0, "error": 0}
    for i, msg_id in enumerate(message_ids):
        try:
            msg = (
                service.users()
                .messages()
                .get(userId="me", id=msg_id, format="full")
                .execute()
            )

            payload = msg.get("payload", {})
            headers = {h["name"].lower(): h["value"]
                       for h in payload.get("headers", [])}
            subject = headers.get("subject", "(no subject)")
            sender = headers.get("from", "")

            if _should_skip_email(subject, sender, headers):
                skipped_counts["non_newsletter"] += 1
                print(
                    f"  [{i + 1}/{len(message_ids)}] SKIPPED (not a newsletter)")
                print(f"    Subject: {subject[:60]}")
                print(f"    From: {sender[:50]}")
                continue

            newsletter = _parse_message(msg)
            if newsletter:
                newsletters.append(newsletter)
                print(
                    f"  [{i + 1}/{len(message_ids)}] {newsletter.subject[:70]}")
                print(
                    f"    From: {newsletter.sender[:50]}  |  Body: {len(newsletter.body)} chars"
                )
            else:
                skipped_counts["too_short"] += 1
                print(f"  [{i + 1}/{len(message_ids)}] SKIPPED (body too short)")
                print(f"    Subject: {subject[:60]}")
        except Exception as e:
            skipped_counts["error"] += 1
            print(f"  [{i + 1}/{len(message_ids)}] ERROR: {e}")

    total_skipped = sum(skipped_counts.values())
    print(f"\n  Fetched: {len(newsletters)} newsletters")
    if total_skipped > 0:
        print(f"  Skipped: {total_skipped} total")
        if skipped_counts["non_newsletter"] > 0:
            print(
                f"    - {skipped_counts['non_newsletter']} non-newsletters (GitHub, alerts, etc.)"
            )
        if skipped_counts["too_short"] > 0:
            print(f"    - {skipped_counts['too_short']} too short")
        if skipped_counts["error"] > 0:
            print(f"    - {skipped_counts['error']} errors")

    _save_last_fetch(datetime.now(timezone.utc))

    return newsletters

def fetch_newsletter_by_id(service, message_id: str) -> Newsletter | None:
    if not message_id:
        return None
    msg = (
        service.users()
        .messages()
        .get(userId="me", id=message_id, format="full")
        .execute()
    )
    return _parse_message(msg)

def message_id_from_gmail_url(gmail_url: str) -> str:
    if not gmail_url:
        return ""
    marker = "#inbox/"
    if marker not in gmail_url:
        return ""
    return gmail_url.split(marker, 1)[1].strip()

def normalize_email_text(text: str) -> str:
    if not text:
        return ""
    text = html.unescape(text)
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"[\u00ad\u200b-\u200f\u2060\ufeff]", "", text)
    text = re.sub(r"[ \t\u2000-\u200a\u202f\u205f\u3000]+", " ", text)
    text = re.sub(r"(?:[ℏ¬]+\s*){8,}", " ", text)
    text = re.sub(r"\n\s*\n\s*\n+", "\n\n", text)
    lines = []
    for raw_line in text.split("\n"):
        line = raw_line.strip()
        if not line:
            if lines and lines[-1] != "":
                lines.append("")
            continue
        visible = re.sub(r"[^A-Za-z0-9]+", "", line)
        if not visible and len(line) >= 8:
            continue
        lines.append(line)
    cleaned = "\n".join(lines).strip()
    return cleaned

def _should_skip_email(
        subject: str,
        sender: str,
        headers: dict | None = None) -> bool:
    subject_lower = subject.lower()
    sender_lower = sender.lower()

    if "notifications@github.com" in sender_lower or "github.com" in sender_lower:
        return True

    if "no-reply@accounts.google.com" in sender_lower:
        return True

    transactional_senders = [
        "splitwise.com",
        "venmo.com",
        "paypal.com",
        "zomato.com",
        "swiggy.com",
        "uber.com",
        "ola.com",
        "amazon.com",
        "flipkart.com",
        "phonepe.com",
        "paytm.com",
    ]
    if any(s in sender_lower for s in transactional_senders):
        return True

    skip_senders = [
        "noreply@",
        "no-reply@",
        "notifications@",
        "alerts@",
        "security@",
        "support@",
        "donotreply@",
    ]
    if any(pattern in sender_lower for pattern in skip_senders):
        newsletter_services = [
            "substack",
            "beehiiv",
            "mailchimp",
            "convertkit"]
        if not any(service in sender_lower for service in newsletter_services):
            return True

    if subject_lower.startswith("re:") or subject_lower.startswith("fwd:"):
        return True

    skip_subjects = [
        "security alert",
        "verify your email",
        "password reset",
        "confirm your",
        "your order",
        "your receipt",
        "invoice",
        "payment",
        "verification code",
        "one-time password",
        "otp",
        "installment",
        "account statement",
        "payment due",
        "amount due",
        "bill reminder",
        "billing reminder",
        "transaction alert",
        "debit alert",
        "credit alert",
        "bank statement",
        "your booking",
        "booking confirmation",
        "your appointment",
        "shipment",
        "dispatched",
        "out for delivery",
        "delivered",
        "your balance",
        "balance for",
        "final reminder",
        "last reminder",
        "thank-you offer",
        "expiring soon",
        "expires today",
        "offer ends",
    ]
    if any(pattern in subject_lower for pattern in skip_subjects):
        return True

    if headers:
        has_newsletter_header = (
            "list-unsubscribe" in headers
            or "list-id" in headers
            or "list-post" in headers
            or headers.get("precedence", "").lower() in ("bulk", "list")
        )
        transactional_hints = [
            "reminder", "alert", "notice", "confirmation",
            "statement", "account", "transaction", "due",
        ]
        if not has_newsletter_header and any(
                h in subject_lower for h in transactional_hints):
            return True

    return False

def _build_newsletter_query(since: datetime) -> str:
    from datetime import timedelta
    query_date = since - timedelta(days=1)
    date_str = query_date.strftime("%Y/%m/%d")
    return f"after:{date_str} -in:spam -in:trash"

def _parse_message(msg: dict) -> Newsletter | None:
    payload = msg.get("payload", {})
    headers = {h["name"].lower(): h["value"]
               for h in payload.get("headers", [])}

    subject = headers.get("subject", "(no subject)")
    sender = headers.get("from", "")
    date = headers.get("date", "")
    snippet = msg.get("snippet", "")

    body = _extract_body(payload)
    if not body or len(body.strip()) < 50:
        return None

    raw_html = _collect_html(payload)
    links = []
    images = []
    if raw_html:
        try:
            from digest.link_extractor import extract_links_from_html
            links = extract_links_from_html(raw_html)
        except Exception:
            links = []
        try:
            from digest.image_extractor import extract_images_from_html
            images = extract_images_from_html(raw_html)
        except Exception:
            images = []

    return Newsletter(
        id=msg["id"],
        subject=normalize_email_text(subject),
        sender=sender,
        date=date,
        body=normalize_email_text(body),
        snippet=snippet,
        links=links,
        images=images,
    )

def _collect_html(payload: dict) -> str:
    mime = payload.get("mimeType", "")
    data = payload.get("body", {}).get("data")
    if data and mime == "text/html":
        try:
            return base64.urlsafe_b64decode(
                data +
                "==").decode(
                "utf-8",
                errors="replace")
        except Exception:
            return ""
    for part in payload.get("parts", []) or []:
        html = _collect_html(part)
        if html:
            return html
    return ""

def _extract_body(payload: dict) -> str:
    mime_type = payload.get("mimeType", "")

    body_data = payload.get("body", {}).get("data")
    if body_data:
        decoded = base64.urlsafe_b64decode(body_data + "==").decode(
            "utf-8", errors="replace"
        )
        if "html" in mime_type:
            return _clean_html(decoded)
        return decoded

    parts = payload.get("parts", [])
    if not parts:
        return ""

    plain_text = ""
    html_text = ""

    for part in parts:
        part_mime = part.get("mimeType", "")
        part_data = part.get("body", {}).get("data")

        if part_mime == "text/plain" and part_data:
            plain_text = base64.urlsafe_b64decode(part_data + "==").decode(
                "utf-8", errors="replace"
            )
        elif part_mime == "text/html" and part_data:
            decoded = base64.urlsafe_b64decode(part_data + "==").decode(
                "utf-8", errors="replace"
            )
            html_text = _clean_html(decoded)
        elif part.get("parts"):
            nested_text = _extract_body(part)
            if nested_text and not plain_text:
                plain_text = nested_text

    if plain_text and html_text:
        return _choose_preferred_body(plain_text, html_text)
    if plain_text:
        return plain_text
    if html_text:
        return html_text
    return ""

def _clean_html(html: str) -> str:
    text = re.sub(r"<style[^>]*>[\s\S]*?</style>",
                  "", html, flags=re.IGNORECASE)
    text = re.sub(r"<script[^>]*>[\s\S]*?</script>",
                  "", text, flags=re.IGNORECASE)
    text = re.sub(r"</(div|p|li|tr|h[1-6])>", "\n", text, flags=re.IGNORECASE)
    text = re.sub(r"<br[^>]*>", "\n", text, flags=re.IGNORECASE)
    text = re.sub(r"<[^>]+>", "", text)
    text = text.replace("&nbsp;", " ").replace("&amp;", "&")
    text = text.replace("&lt;", "<").replace("&gt;", ">")
    text = text.replace("&quot;", '"').replace("&#39;", "'")
    text = re.sub(r"\n\s*\n+", "\n\n", text)
    return text.strip()

def _choose_preferred_body(
    plain_text: str, html_text: str
) -> str:
    plain_score = _score_body_candidate(plain_text)
    html_score = _score_body_candidate(html_text)
    plain_is_stub = _looks_like_plaintext_stub(plain_text)
    html_is_stub = _looks_like_plaintext_stub(html_text)

    if plain_is_stub and not html_is_stub and len(
            html_text) >= len(plain_text) * 2:
        return html_text

    if html_score > plain_score:
        return html_text

    return plain_text

def _score_body_candidate(text: str) -> int:
    normalized = text.lower()
    newline_count = text.count("\n")
    url_count = len(re.findall(r"https?://[^\s]+", text))
    sentence_like = len(re.findall(r"[.!?](?:\s|$)", text))
    boilerplate_hits = sum(
        1 for pattern in _stub_patterns() if pattern in normalized)

    score = len(text)
    score += min(newline_count, 300) * 12
    score += min(sentence_like, 120) * 25
    score -= boilerplate_hits * 2500
    score -= url_count * 120
    return score

def _looks_like_plaintext_stub(text: str) -> bool:
    normalized = text.lower()
    if len(text) < 350 and any(
            pattern in normalized for pattern in _stub_patterns()):
        return True

    url_count = len(re.findall(r"https?://[^\s]+", text))
    if len(text) < 1200 and url_count >= 3:
        return True
    return False

def _stub_patterns() -> tuple[str, ...]:
    return (
        "you are reading a plain text version",
        "view this post on the web",
        "copy and paste this link",
        "read online",
        "if you cannot see this email",
        "view in browser",
    )

def _load_last_fetch() -> datetime | None:
    if LAST_FETCH_PATH.exists():
        with open(LAST_FETCH_PATH, "r") as f:
            data = json.load(f)
            return datetime.fromisoformat(data["last_fetch_iso"])
    return None

def _save_last_fetch(timestamp: datetime):
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    with open(LAST_FETCH_PATH, "w") as f:
        json.dump({"last_fetch_iso": timestamp.isoformat()}, f, indent=2)
