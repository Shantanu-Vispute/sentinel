import json
import time
import re
from dataclasses import dataclass
from config import SENTINEL_PII_PATTERNS
from digest.gmail_fetcher import Newsletter
import digest.llm_client as llm_client

_PROMO_SUBJECT_PATTERNS = re.compile(
    r"\bwebinar\b|\bregister (for|now)\b|^welcome to\b|last chance|final reminder|last reminder"
    r"|join us (for|at)\b|\binvitation\b|offer ends|expiring soon|expires today|thank.you offer"
    r"|\byour balance\b|\bbalance for\b", re.IGNORECASE, )

def _is_promotional(newsletter: Newsletter) -> bool:
    if _PROMO_SUBJECT_PATTERNS.search(newsletter.subject):
        return True

    if len(newsletter.body) < 800:
        return True
    return False

_PII_PATTERNS = [
    re.compile(pattern.strip(), re.IGNORECASE)
    for pattern in SENTINEL_PII_PATTERNS
    if pattern.strip()
]

def _scrub_pii(text: str) -> str:
    for pattern in _PII_PATTERNS:
        text = pattern.sub('[REDACTED]', text)
    return text

SYSTEM_PROMPT = """You are a news story extractor.

Return JSON only with this schema:
    {"stories":[{"title":"...","summary":"..."}]}

CONTENT TYPE DETECTION — decide before extracting:

    CURATED DIGEST: Multiple unrelated news items or events bundled together
  (e.g., "5 things happening this week", "today's top stories", roundup newsletters).
  → Output ONE story per independent item, including ALL sections:
    - Main featured stories (with full body text)
    - "Other news" / secondary headlines (headline-only entries)
    - Tools, products, papers, and reports sections
  → For items with only a headline and no body, use the headline text as the summary.
    Do NOT hallucinate details — only use what is explicitly present.

SINGLE ARTICLE: One author writing about ONE topic, even if it has multiple
  sections, headings, chapters, or sub-points explaining that same topic.
  This includes tutorials, deep-dives, opinion pieces, and explainers.
  → Output EXACTLY ONE story covering the whole piece.

KEY TEST — ask yourself: are the sections INDEPENDENT NEWS EVENTS, or SUB-POINTS of one topic?
  INDEPENDENT events (curated digest, multiple stories):
    "Company A raises $10B", "Company B releases a new model", "Company C lays off 100 engineers"
    Each section links to a different external article or news source.
  SUB-POINTS of one topic (single article, one story):
    "Loss Functions", "Gradient Descent", "Next-Token Prediction" (all explain how LLMs learn)
    "The Incident", "The Response", "The Aftermath" (all about the same event)
    "Why It Matters", "How It Works", "What's Next" (all about the same subject)
    "Data vs hype", "Building world-class orgs", "Future with Martin Fowler" (all sections
      in one author's conference recap covering different speakers at the same event)

CONFERENCE RECAPS & EVENT SUMMARIES: When one author writes about multiple talks,
  presentations, or discussions they attended or organized (e.g., summit notes, workshop
  recap, event writeup), this is ALWAYS one story — even with numbered sections per speaker.

CRITICAL RULE: Section headings or H2/H3 headers within a single article do NOT make it
a curated digest. A tutorial with 5 chapters is still ONE story. A conference recap with
3 numbered sessions is still ONE story.

NEVER extract these as stories (return no story for them):
    - Sponsor blocks, paid promotions, or "brought to you by" sections
- Self-promotional sections advertising the newsletter's own courses, products, or services
- "Advertise with us" / "reach N,000 readers" sections
- Account summaries, balance notifications, financial transaction emails
- Course listings or resource directories that are clearly the newsletter's own offerings
- Countdown offers, "last chance" reminders, or discount CTAs

"Tools sections" means independent external tools/products covered as news — NOT the newsletter's own course catalog or sponsor placements.

Strict rules:
    - Use only facts explicitly present in the provided content.
- Do not hallucinate names, numbers, dates, or background context.
- Do NOT ignore secondary news sections, "other news" lists, tools sections, or papers/reports about external products/projects.
- Titles must be specific story headlines from content, not meta labels.
- Never output classification/meta titles such as:
  "Curated digest", "Single-topic article", "Type A", "Type B", "Newsletter overview".
- Summaries must describe the story itself (2-3 sentences), not the format of the newsletter.

If no extractable news story exists (e.g. purely transactional, promotional, or account notification email), return {"stories":[]}.
"""

@dataclass
class Story:
    title: str
    summary: str
    primary_url: str = ""
    source_newsletter: str = ""
    source_email_body: str = ""
    source_sender: str = ""
    source_gmail_url: str = ""
    date: str = ""

def _extract_sender_name(from_field: str) -> str:
    if "<" in from_field:
        name = from_field.split("<")[0].strip()
        return name if name else from_field
    return from_field

def _strip_urls(text: str) -> str:
    text = re.sub(r"https?://[^\s]+", "", text)

    text = re.sub(r"www\.[^\s]+", "", text)

    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n[ \t]+", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()

def extract_stories(newsletters: list[Newsletter]) -> list[Story]:
    all_stories = []
    total_time = 0

    skipped = 0
    for i, newsletter in enumerate(newsletters):
        print(
            f"\n  [{i + 1}/{len(newsletters)}] Processing: {newsletter.subject}")
        print(f"    From:    {newsletter.sender}")
        print(f"    Date:    {newsletter.date}")
        print(f"    Body:    {len(newsletter.body)} chars")
        print(f"    URL:     {newsletter.gmail_url}")

        if _is_promotional(newsletter):
            print(f"    SKIP:    Promotional/transactional email, skipping")
            skipped += 1
            continue

        start = time.time()
        try:
            stories = _extract_from_single(newsletter)

            elapsed = time.time() - start
            total_time += elapsed

            all_stories.extend(stories)
            print(
                f"    Result:  {
                    len(stories)} stories extracted ({
                    elapsed:.1f}s)")

            for j, story in enumerate(stories):
                print(f"      [{j + 1}] {story.title}")
                print(f"          Summary:  {story.summary[:120]}...")

        except Exception as e:
            elapsed = time.time() - start
            total_time += elapsed
            print(f"    ERROR:   {e} ({elapsed:.1f}s)")

    processed = len(newsletters) - skipped
    print(
        f"\n  Extraction complete: {len(all_stories)} stories from {processed} emails "
        f"({skipped} skipped as promotional) ({total_time:.1f}s total)"
    )

    return all_stories

def _extract_from_single(newsletter: Newsletter) -> list[Story]:
    clean_text = _scrub_pii(_strip_urls(newsletter.body))

    prompt = (
        f"Extract news stories from this newsletter.\n\n"
        f"IMPORTANT: If this is a single article (one author writing about one theme, "
        f"even with multiple numbered sections, speaker summaries, or sub-topics), "
        f"output EXACTLY ONE story. Conference recaps, event summaries, deep-dives, "
        f"tutorials, and opinion pieces are always ONE story. "
        f"Only output multiple stories if this is a curated digest where each item "
        f"independently links to or summarizes a different external source.\n\n"
        f"IMPORTANT: Only use facts from the text below. Do NOT invent or add background details not in the source.\n\n"
        f"Newsletter: {_scrub_pii(newsletter.subject)}\n"
        f"From: {_scrub_pii(newsletter.sender)}\n"
        f"Date: {newsletter.date}\n\n"
        f"Content:\n{clean_text}"
    )

    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": prompt},
    ]

    format_schema = {
        "type": "object",
        "properties": {
            "stories": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "title": {"type": "string"},
                        "summary": {"type": "string"},
                    },
                    "required": ["title", "summary"],
                },
            }
        },
        "required": ["stories"],
    }

    response = llm_client.chat(messages=messages, format_schema=format_schema)
    raw_response = response.message.content

    try:
        parsed = _parse_response(json.loads(raw_response), newsletter)
    except json.JSONDecodeError:
        print("    WARN: JSON parse failed")
        parsed = []

    try:
        from digest.link_extractor import pick_primary_url
        sender_host = ""
        sender_email = newsletter.sender or ""
        m = re.search(r"[\w.+-]+@([\w-]+\.[\w.-]+)", sender_email)
        if m:
            sender_host = m.group(1).lower()
        links = getattr(newsletter, "links", None) or []
        for story in parsed:
            url = pick_primary_url(
                story.title,
                story.summary,
                links,
                sender_host=sender_host)
            if url:
                story.primary_url = url
    except Exception as _e:
        pass

    return parsed

def _parse_response(result: dict, newsletter: Newsletter) -> list[Story]:
    stories = []
    banned_titles = {
        "curated digest",
        "single-topic article",
        "single topic article",
        "type a",
        "type b",
        "newsletter overview",
        "newsletter summary",
    }
    for item in result.get("stories", []):
        title = item.get("title", "").strip()
        summary = item.get("summary", "").strip()

        if not title or not summary:
            continue
        if title.lower() in banned_titles:
            continue

        stories.append(
            Story(
                title=title,
                summary=summary,
                source_newsletter=newsletter.subject,
                source_email_body=newsletter.body,
                source_sender=_extract_sender_name(newsletter.sender),
                source_gmail_url=newsletter.gmail_url,
                date=newsletter.date,
            )
        )

    return stories
