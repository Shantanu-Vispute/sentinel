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

SYSTEM_PROMPT = """You are a newsletter story extractor.

Return JSON only with this schema:
    {"content_type":"curated_digest|single_article|promotional","stories":[{"title":"...","summary":"..."}]}

Follow this order:

1. Classify the email.
2. Extract stories using only the rule for that class.

CONTENT TYPES:

curated_digest:
- Multiple independent news items, launches, papers, tools, funding events, policy updates,
  or links are bundled together.
- Examples: "today's top stories", "5 things happening this week", roundup newsletters,
  link digests, tools/papers sections about external products or projects.
- Extract one story per independent item.
- Include main stories, secondary headlines, "other news", tools, products, papers,
  and reports when they are external/newsworthy.
- For headline-only items, use the headline text as the summary. Do not add details.
- Never create one generic story for a whole section such as "Other news",
  "Other news & articles", "Trending tools", or "Trending papers & reports".
  Split those sections into specific item-level stories when each item has enough
  explicit detail. If the section only lists names/links without enough substance,
  skip the thin items.

single_article:
- One author is writing about one main theme, argument, tutorial, event, recap, or explainer.
- It is still one story even when it has sections, numbered points, speaker summaries,
  chapters, sub-topics, or headings.
- Examples: tutorials, deep-dives, opinion pieces, explainers, conference recaps,
  event summaries, workshop notes, one essay covering multiple speakers at the same event.
- Extract exactly one story covering the whole piece.

promotional:
- The email is mostly transactional, promotional, account-related, a sponsor block,
  a course catalog, a resource directory for the newsletter's own products, an ad,
  or a discount/last-chance CTA.
- Extract no stories.

DECISION TEST:
- If sections are independent events or external links that could stand alone as separate
  news cards, use curated_digest.
- If sections are sub-points supporting one article, one authorial argument, or one event
  recap, use single_article.
- Section headings alone never make an email a curated digest.

NEVER extract these as stories:
- Sponsor blocks, paid promotions, or "brought to you by" sections.
- Self-promotional sections advertising the newsletter's own courses, products, or services.
- "Advertise with us" / "reach N,000 readers" sections.
- Account summaries, balance notifications, financial transaction emails.
- Course listings or resource directories that are clearly the newsletter's own offerings.
- Countdown offers, "last chance" reminders, or discount CTAs.

Strict rules:
- Use only facts explicitly present in the provided content.
- Do not hallucinate names, numbers, dates, or background context.
- Do not ignore secondary news sections when the email is a curated_digest.
- Titles must be specific story headlines from content, not meta labels.
- Never output classification/meta titles such as:
  "Curated digest", "Single-topic article", "Type A", "Type B", "Newsletter overview",
  "Other news", "Other news & articles", "Trending tools", "Trending papers & reports",
  "AI findings and resources", "Industry news and developments".
- Summaries must describe the story itself (2-3 sentences), not the format of the newsletter.
- If content_type is promotional, return an empty stories array.
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
        f"Classify this email first, then extract stories using the matching rule.\n"
        f"Return JSON only. Use content_type=curated_digest for independent news/link roundups, "
        f"content_type=single_article for one article/essay/recap/tutorial, and "
        f"content_type=promotional when there is no extractable news story.\n\n"
        f"Use only facts from the text below. Do not invent or add background details.\n\n"
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
            "content_type": {
                "type": "string",
                "enum": ["curated_digest", "single_article", "promotional"],
            },
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
        "required": ["content_type", "stories"],
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
        "other news",
        "other news & articles",
        "trending tools",
        "trending papers & reports",
        "ai findings and resources",
        "industry news and developments",
        "new ai tools and resources",
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
