from digest.gmail_auth import get_gmail_service
from config import MAX_FETCH_RESULTS, SIMILARITY_THRESHOLD
import digest.llm_client as llm_client
from digest.media_cache import cache_remote_image
from digest.pii import scrub_pii
from digest.storage import StoryDB
from digest.story_extractor import extract_stories
from digest.story_quality import assess_story_quality
from digest.gmail_fetcher import fetch_newsletters
import fcntl
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

_LOCK_DIR = Path(__file__).resolve().parent.parent / "state"
_lock_fh = None
_lock_path: Path | None = None

def _acquire_lock(name: str = "daemon"):
    global _lock_fh, _lock_path
    _LOCK_DIR.mkdir(exist_ok=True)
    _lock_path = _LOCK_DIR / f"{name}.lock"
    _lock_fh = open(_lock_path, "a+")
    try:
        fcntl.flock(_lock_fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        _lock_fh.seek(0)
        _lock_fh.truncate()
        _lock_fh.write(str(os.getpid()))
        _lock_fh.flush()
    except OSError:
        print(f"Another {name} instance is already running. Exiting.")
        sys.exit(75)

def _release_lock():
    if _lock_fh:
        fcntl.flock(_lock_fh, fcntl.LOCK_UN)
        _lock_fh.close()
    if _lock_path:
        _lock_path.unlink(missing_ok=True)

NEW_INFO_PROMPT = """\
You are checking if a new news report is about the same topic as an existing story AND adds new information.

Existing story title: {existing_title}
Existing story summary:
    {existing_summary}

New report title: {new_title}
New report summary: {new_summary}

First, decide: is the new report about the SAME specific topic, event, or entity as the existing story?
- "Company A raises funding" and "Company A valuation details" → same topic
- "AI slide deck tool" and "AI voice transcription tool" → different topics (both are AI tools but unrelated)
- "Claude Cowork launch" and "Claude Cowork adds connectors" → same topic

If they are about different topics, set adds_new_info to false regardless of content.
Only if same topic: does the new report add facts, numbers, or developments not in the existing summary?

Answer with JSON only:
    {{
  "adds_new_info": true/false,
  "what_changed": "one sentence describing what is new, or empty string if nothing new or different topic",
  "updated_title": "if adds_new_info is true and the existing title is stale or less precise: a concise canonical title for the same story. Keep empty if no title change is needed, or if a better title would broaden the story into an umbrella topic.",
  "updated_summary": "if adds_new_info is true: a clean 2-4 sentence summary that rewrites the existing summary to incorporate the new information naturally. If adds_new_info is false: empty string."
}}

The updated_title must still describe the same specific story, not a wider product family,
company theme, event recap, or related-story bundle.
The updated_summary must read as a single coherent paragraph — do NOT concatenate or append sentences."""

def _check_new_info(
        existing_title: str,
        existing_summary: str,
        new_title: str,
        new_summary: str) -> tuple[bool, str, str, str]:
    prompt = NEW_INFO_PROMPT.format(
        existing_title=existing_title,
        existing_summary=existing_summary,
        new_title=new_title,
        new_summary=new_summary,
    )
    schema = {
        "type": "object",
        "properties": {
            "adds_new_info": {"type": "boolean"},
            "what_changed": {"type": "string"},
            "updated_title": {"type": "string"},
            "updated_summary": {"type": "string"},
        },
        "required": ["adds_new_info", "what_changed", "updated_title", "updated_summary"],
    }
    try:
        response = llm_client.chat(
            messages=[{"role": "user", "content": prompt}],
            format_schema=schema,
        )
        raw = response.message.content
        result = json.loads(raw)
        adds_new_info = result.get("adds_new_info", False)
        what_changed = result.get("what_changed", "")
        updated_title = result.get("updated_title", "")
        updated_summary = result.get("updated_summary", "")

        return adds_new_info, what_changed, updated_title, updated_summary
    except Exception as e:
        print(f"        WARN: new-info check failed: {e}")
        return False, "", "", ""

def _check_backends() -> bool:
    checks = llm_client.get_llm_client().healthcheck()
    ok = True
    for check in checks:
        if check.ok:
            detail = f" ({check.detail})" if check.detail else ""
            print(f"      OK: {check.label}{detail}")
            continue
        ok = False
        detail = f" ({check.detail})" if check.detail else ""
        print(f"      ERROR: {check.label}{detail}")
    return ok

def _persist_story(
        db,
        story,
        new_count,
        merged_count,
        evolved_count,
        source_type: str = "email",
        skip_new_info_check: bool = False):
    quality = assess_story_quality(
        story.title,
        story.summary,
        source_type=source_type,
        sender=story.source_sender,
    )
    if quality.should_skip_ingestion:
        print(f"      SKIP: {quality.reason}")
        return new_count, merged_count, evolved_count, True

    try:
        emb_resp = llm_client.embed(
            input_text=f"{
                story.title}. {
                story.summary}")
        embedding = emb_resp.embeddings[0]
    except Exception as e:
        print(f"      WARN: embed failed: {e}")
        return new_count, merged_count, evolved_count, False

    existing_id = db.find_similar(embedding, threshold=SIMILARITY_THRESHOLD)

    if existing_id:
        existing = db.get_story(existing_id)
        adds_new_info = False
        what_changed = ""
        updated_title = ""
        updated_summary = ""
        if not skip_new_info_check and existing and existing["mention_count"] >= 2:
            adds_new_info, what_changed, updated_title, updated_summary = _check_new_info(
                existing["title"], existing["summary"], story.title, story.summary,
            )

        db.add_mention(
            story_id=existing_id,
            title=story.title,
            summary=story.summary,
            sender=story.source_sender,
            gmail_url=story.source_gmail_url,
            date=story.date,
            added_new_info=adds_new_info,
            source_type=source_type,
            raw_title=story.source_newsletter,
            raw_body=story.source_email_body,
        )

        if adds_new_info and what_changed:
            new_summary = updated_summary if updated_summary else f"{
                existing['summary']} {what_changed}"
            db.update_story_summary(existing_id, new_summary.strip())
            updated_title = (updated_title or "").strip()
            if updated_title and updated_title != existing.get("title"):
                db.update_story_title(existing_id, updated_title)
            db.add_timeline_entry(
                story_id=existing_id,
                date=story.date,
                what_changed=what_changed,
                trigger_sender=story.source_sender,
            )
            db.increment_new_info_count(existing_id)
            if existing and existing.get("is_read"):
                db.mark_unread(existing_id)
            evolved_count += 1
            print(f"      → MERGED + EVOLVED: {what_changed[:60]}")
        else:
            merged_count += 1
            print(f"      → MERGED into [{existing_id[:8]}]")
    else:
        primary_image = getattr(story, "primary_image_url", "")
        if primary_image:
            try:
                primary_image = cache_remote_image(primary_image, source_type)
            except Exception as exc:
                print(f"      image cache failed: {exc}")
                primary_image = ""

        story_id = db.add_story(
            title=story.title,
            summary=story.summary,
            entities=[],
            embedding=embedding,
            sender=story.source_sender,
            gmail_url=story.source_gmail_url,
            date=story.date,
            mention_title=story.title,
            mention_summary=story.summary,
            mention_raw_title=story.source_newsletter,
            mention_raw_body=story.source_email_body,
            category="other",
            primary_url=getattr(story, "primary_url", ""),
            primary_image_url=primary_image,
            source_type=source_type,
        )
        new_count += 1
        print(f"      → NEW [{story_id[:8]}]")

    return new_count, merged_count, evolved_count, True

def process_new_emails(max_results: int = 50, since: datetime | None = None):
    print("=" * 60)
    print(
        f"Sentinel Email Ingestion  [{
            datetime.now().strftime('%Y-%m-%d %H:%M')}]")
    print("=" * 60)

    llm_client.get_llm_client()

    print("\n[0/3] Checking configured model backends...")
    if not _check_backends():
        print(f"      Aborting — no emails fetched, no LLM calls made.")
        return

    db = StoryDB()

    print("\n[1/3] Fetching emails...")
    service = get_gmail_service()
    newsletters = fetch_newsletters(
        service, since=since, max_results=max_results)
    print(f"      {len(newsletters)} newsletters fetched")

    new_newsletters = [
        nl for nl in newsletters if not db.is_email_processed(
            nl.id)]
    already_done = len(newsletters) - len(new_newsletters)
    if already_done:
        print(f"      {already_done} already processed — skipping")
    newsletters = new_newsletters

    if not newsletters:
        print("      Nothing new to process.")
        db.close()
        return

    print(f"\n[2/3] Processing {len(newsletters)} emails...")
    new_count = merged_count = evolved_count = 0

    for ei, newsletter in enumerate(newsletters, 1):
        print(f"\n  ── [{ei}/{len(newsletters)}] {newsletter.subject[:65]}")

        email_stories, extraction_failed = extract_stories([newsletter])
        if extraction_failed:
            print(f"      extraction failed — leaving for next run")
            continue
        if not email_stories:
            db.mark_email_processed(newsletter.id)
            continue

        persist_ok = True
        for story in email_stories:
            print(f"    · {story.title[:65]}...")
            new_count, merged_count, evolved_count, ok = _persist_story(
                db, story, new_count, merged_count, evolved_count
            )
            persist_ok = persist_ok and ok

        if persist_ok:
            db.mark_email_processed(newsletter.id)
        else:
            print(f"      one or more stories failed to persist — leaving for next run")

    print(f"\n[3/3] Done")
    print(f"      New stories:    {new_count}")
    print(f"      Merged:         {merged_count}")
    print(f"      Evolved:        {evolved_count}")

    stats = db.get_stats()
    print(f"\n      DB totals:")
    print(f"      - Stories:   {stats['total_stories']}")
    print(f"      - Mentions:  {stats['total_mentions']}")
    print(f"      - Unread:    {stats['unread']}")
    print(f"      - Evolved:   {stats['evolved']}")

    db.close()

_TITLE_SKIP_RE = re.compile(r"^[\W\d_]+$")
_URL_RE = re.compile(r"https?://\S+")
_WS_RE = re.compile(r"\s+")

def _tg_content_hash(text: str) -> str:
    import hashlib
    normalized = _URL_RE.sub("", (text or "").lower())
    normalized = _WS_RE.sub(" ", normalized).strip()[:500]
    return hashlib.sha1(normalized.encode(
        "utf-8")).hexdigest() if normalized else ""

def _tg_post_to_story(post, translated_text: str):
    from digest.story_extractor import Story

    lines = [ln.strip() for ln in translated_text.splitlines() if ln.strip()]

    substantive = [ln for ln in lines if not _TITLE_SKIP_RE.match(ln)]
    title = (substantive[0] if substantive else (
        lines[0] if lines else f"@{post.channel} post"))[:140]
    primary_url = post.links[0] if post.links else post.url

    return Story(
        title=title,
        summary=translated_text,
        primary_url=primary_url,
        source_newsletter=f"@{post.channel}",
        source_sender=f"@{post.channel}",
        source_gmail_url=post.url,
        date=post.date,
    )

def process_telegram_posts(since: datetime | None = None):
    from ingest.telegram_fetcher import fetch_channels
    from config import TELEGRAM_CHANNELS

    print("=" * 60)
    print(f"Sentinel Telegram Ingestion  [{datetime.now().strftime('%Y-%m-%d %H:%M')}]")
    if since:
        print(f"  backfilling since: {since.isoformat()}")
    print("=" * 60)

    if not TELEGRAM_CHANNELS:
        print("No channels configured (TELEGRAM_CHANNELS in .env). Exiting.")
        return

    db = StoryDB()

    print(f"\n[1/3] Fetching {len(TELEGRAM_CHANNELS)} channel(s)...")
    posts = fetch_channels(TELEGRAM_CHANNELS, since=since)
    print(f"      {len(posts)} posts fetched")

    new_posts = [p for p in posts if not db.is_email_processed(p.id)]
    already = len(posts) - len(new_posts)
    if already:
        print(f"      {already} already processed — skipping")

    filtered = [p for p in new_posts if p.is_sponsored or p.is_digest]
    new_posts = [p for p in new_posts if not (p.is_sponsored or p.is_digest)]
    if filtered:
        ads = sum(1 for p in filtered if p.is_sponsored)
        digests = sum(
            1 for p in filtered if p.is_digest and not p.is_sponsored)
        print(
            f"      filtered: {ads} sponsored + {digests} multi-topic roundups — skipping")
        for p in filtered:
            db.mark_email_processed(p.id)

    if not new_posts:
        print("      Nothing new to process.")
        db.close()
        return

    print(f"\n[2/3] Processing {len(new_posts)} posts...")
    new_count = 0

    duplicate_count = 0

    for i, post in enumerate(new_posts, 1):
        print(f"\n  ── [{i}/{len(new_posts)}] @{post.channel}/{post.msg_id}")
        if not post.text.strip():
            print(f"      (no text — media-only post, skipping)")
            db.mark_email_processed(post.id)
            continue

        post_text = scrub_pii(post.text)

        chash = _tg_content_hash(post_text)
        if chash:
            dup_id = db.find_by_content_hash(chash)
            if dup_id:
                print(
                    f"      duplicate of [{dup_id[:8]}] — skipping (cross-channel repost)")
                db.mark_email_processed(post.id)
                duplicate_count += 1
                continue

        from digest.translator import needs_translation, translate as _translate_fn
        if needs_translation(post_text):
            translated = _translate_fn(post_text)
            if translated is None:
                print(f"      translation failed — leaving for next run")
                continue
            print(
                f"      translated ({len(post_text)} → {len(translated)} chars)")
        else:
            translated = post_text

        story = _tg_post_to_story(post, translated)
        quality = assess_story_quality(
            story.title,
            story.summary,
            source_type="telegram",
            sender=story.source_sender,
        )
        if quality.should_skip_ingestion:
            print(f"      SKIP: {quality.reason}")
            db.mark_email_processed(post.id)
            continue

        primary_image = ""
        if post.images:
            primary_image = post.images[0]
        elif post.video_thumbs:
            primary_image = post.video_thumbs[0]
        if primary_image:
            try:
                primary_image = cache_remote_image(primary_image, "telegram")
            except Exception as exc:
                print(f"      image cache failed: {exc}")

        story_id = db.add_story(
            title=story.title,
            summary=story.summary,
            entities=[],
            embedding=None,
            sender=story.source_sender,
            gmail_url=story.source_gmail_url,
            date=story.date,
            mention_title=story.title,
            mention_summary=story.summary,
            category="other",
            primary_url=story.primary_url,
            source_type="telegram",
            primary_image_url=primary_image,
            links=post.links,
            content_hash=chash,
        )
        print(f"    · {story.title[:65]}...")
        print(f"      → NEW [{story_id[:8]}]")
        new_count += 1
        db.mark_email_processed(post.id)

    print(f"\n[3/3] Done")
    print(f"      New stories:    {new_count}")
    if duplicate_count:
        print(
            f"      Duplicates:     {duplicate_count} (cross-channel reposts skipped)")

    stats = db.get_stats()
    print(f"\n      DB totals:")
    print(f"      - Stories:   {stats['total_stories']}")
    print(f"      - Mentions:  {stats['total_mentions']}")

    db.close()

if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Sentinel ingestion daemon")
    parser.add_argument(
        "--max-results",
        type=int,
        default=MAX_FETCH_RESULTS,
        help=f"Max emails to fetch (default: {MAX_FETCH_RESULTS}, set in .env)")
    parser.add_argument(
        "--since",
        type=str,
        default=None,
        help="Fetch emails since this date (YYYY-MM-DD), e.g. --since 2026-02-20")
    parser.add_argument(
        "--telegram",
        action="store_true",
        help="Fetch and ingest public Telegram channels instead of Gmail")
    parser.add_argument(
        "--telegram-since",
        type=str,
        default=None,
        help="Backfill TG posts from this date (YYYY-MM-DD), e.g. --telegram-since 2026-04-12")
    args = parser.parse_args()

    since = None
    explicit_since = False
    if args.since:
        try:
            since = datetime.strptime(args.since,
                                      "%Y-%m-%d").replace(tzinfo=timezone.utc)
            explicit_since = True
        except ValueError:
            print(f"ERROR: Invalid date '{args.since}'. Use YYYY-MM-DD")
            sys.exit(1)

    fetch_limit = None if explicit_since else args.max_results

    if args.telegram:
        tg_since = None
        if args.telegram_since:
            try:
                tg_since = datetime.strptime(
                    args.telegram_since, "%Y-%m-%d").replace(tzinfo=timezone.utc)
            except ValueError:
                print(
                    f"ERROR: invalid --telegram-since '{args.telegram_since}'. Use YYYY-MM-DD.")
                sys.exit(1)
        _acquire_lock("telegram")
        try:
            process_telegram_posts(since=tg_since)
        finally:
            _release_lock()
        sys.exit(0)

    _acquire_lock()
    try:
        process_new_emails(max_results=fetch_limit, since=since)
    except KeyboardInterrupt:
        print("\nInterrupted")
        sys.exit(0)
    except Exception as e:
        print(f"\nERROR: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)
    finally:
        _release_lock()
