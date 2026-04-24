import argparse
import json
import pathlib
import random
import re
import sqlite3
import sys
import time
from datetime import datetime

from playwright.sync_api import BrowserContext, Page, sync_playwright

HERE = pathlib.Path(__file__).resolve().parent.parent
STATE_DIR = HERE / "state"
PROFILE_DIR = STATE_DIR / "browser_profile"
DB_PATH = STATE_DIR / "stories.db"
LOG_PATH = STATE_DIR / "social.log"

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/133.0.0.0 Safari/537.36"
)

LAUNCH_ARGS = [
    "--start-maximized",
    "--no-sandbox",
    "--disable-blink-features=AutomationControlled",
]

def log(msg: str) -> None:
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    line = f"[{datetime.now().isoformat(timespec='seconds')}] {msg}"
    print(line, flush=True)
    with open(LOG_PATH, "a") as f:
        f.write(line + "\n")

def ensure_schema() -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        """CREATE TABLE IF NOT EXISTS external_bookmarks (
            id TEXT PRIMARY KEY,
            source TEXT NOT NULL,
            url TEXT NOT NULL,
            title TEXT,
            excerpt TEXT,
            cover TEXT,
            author TEXT,
            created_ts INTEGER,
            scraped_at INTEGER NOT NULL,
            last_seen_at INTEGER NOT NULL
        )"""
    )
    cols = {r[1]
            for r in conn.execute("PRAGMA table_info(external_bookmarks)")}
    for col in (
        "avatar",
        "media_json",
        "quoted_json",
        "reply_to",
        "link_card_json",
            "tags"):
        if col not in cols:
            conn.execute(
                f"ALTER TABLE external_bookmarks ADD COLUMN {col} TEXT")
    conn.execute(
        """CREATE TABLE IF NOT EXISTS external_item_state (
            id TEXT PRIMARY KEY,
            is_read INTEGER DEFAULT 0,
            skipped INTEGER DEFAULT 0,
            updated_at TEXT NOT NULL
        )"""
    )
    conn.commit()
    conn.close()

def existing_ids(source: str) -> set[str]:
    conn = sqlite3.connect(DB_PATH)
    rows = conn.execute(
        "SELECT id FROM external_bookmarks WHERE source=?", (source,)).fetchall()
    conn.close()
    return {r[0] for r in rows}

def upsert(items: list[dict]) -> int:
    if not items:
        return 0
    now = int(time.time())
    conn = sqlite3.connect(DB_PATH)
    conn.executemany(
        """INSERT INTO external_bookmarks
               (id, source, url, title, excerpt, cover, author, created_ts, scraped_at, last_seen_at,
                avatar, media_json, quoted_json, reply_to, link_card_json)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT(id) DO UPDATE SET
             title=excluded.title,
             excerpt=excluded.excerpt,
             cover=excluded.cover,
             author=excluded.author,
             avatar=excluded.avatar,
             created_ts=CASE WHEN excluded.created_ts > 0 THEN excluded.created_ts ELSE external_bookmarks.created_ts END,
             media_json=excluded.media_json,
             quoted_json=excluded.quoted_json,
             reply_to=excluded.reply_to,
             link_card_json=excluded.link_card_json,
             last_seen_at=excluded.last_seen_at""",
        [
            (
                it["id"], it["source"], it["url"], it.get("title") or "",
                it.get("excerpt") or "", it.get(
                    "cover") or "", it.get("author") or "",
                it.get("created_ts") or 0, now, now,
                it.get("avatar") or "",
                json.dumps(it.get("media") or []),
                json.dumps(it.get("quoted")) if it.get("quoted") else None,
                it.get("reply_to") or "",
                json.dumps(it.get("link_card")) if it.get(
                    "link_card") else None,
            )
            for it in items
        ],
    )
    conn.commit()
    conn.close()
    return len(items)

def _pause(min_s: float, max_s: float) -> None:
    time.sleep(random.uniform(min_s, max_s))

def _scroll(page: Page) -> None:
    page.evaluate("window.scrollBy(0, 4000)")
    _pause(1.2, 2.0)

def launch(headless: bool = True) -> tuple:
    PROFILE_DIR.mkdir(parents=True, exist_ok=True)
    pw = sync_playwright().start()
    ctx = pw.chromium.launch_persistent_context(
        str(PROFILE_DIR),
        headless=headless,
        args=LAUNCH_ARGS,
        user_agent=USER_AGENT,
        viewport={"width": 1440, "height": 900},
        locale="en-US",
        timezone_id="America/New_York",
    )
    return pw, ctx

def scrape_twitter(
    ctx: BrowserContext,
    full: bool = False,
    max_scrolls: int = 500,
    limit: int | None = None,
) -> list[dict]:
    known = existing_ids("twitter")
    log(f"twitter: starting (known={len(known)}, full={full})")

    page = ctx.new_page()
    log("twitter: navigating to x.com/i/bookmarks")
    page.goto("https://x.com/i/bookmarks", wait_until="domcontentloaded")
    _pause(2.0, 3.5)
    log(f"twitter: landed at {page.url}")

    if "login" in page.url.lower() or "flow/login" in page.url:
        log("twitter: redirected to login — session not persisted. Re-run --login.")
        page.close()
        return []

    try:
        page.wait_for_selector('article[data-testid="tweet"]', timeout=20_000)
        log("twitter: first tweet rendered")
    except Exception:
        try:
            title = page.title()
            body_txt = page.locator("body").inner_text(timeout=2000)[:300]
            log(
                f"twitter: NO tweets after 20s. title={title!r} body-sample={body_txt!r}")
        except Exception:
            log("twitter: NO tweets after 20s and couldn't read page content")
        page.close()
        return []

    seen: set[str] = set()
    collected: list[dict] = []
    repeat_hits = 0
    stable = 0
    last_log = time.time()

    extract_js = r"""
() => {
  function textOrEmpty(el) { return el ? el.innerText : ''; }
  const out = [];
  for (const a of document.querySelectorAll('article[data-testid="tweet"]')) {
    try {
      const timeLink = a.querySelector('a:has(time)');
      const href = timeLink && timeLink.getAttribute('href');
      if (!href || !href.includes('/status/')) continue;
      const tid = href.split('/status/')[1].split('/')[0].split('?')[0];

      let quoteContainer = null;
      for (const el of a.querySelectorAll('div[role="link"][tabindex="0"]')) {
        if (el.querySelector('div[data-testid="User-Name"]')) { quoteContainer = el; break; }
      }

      const allTextEls = a.querySelectorAll('div[data-testid="tweetText"]');
      let text = '';
      for (const t of allTextEls) {
        if (quoteContainer && quoteContainer.contains(t)) continue;
        text = t.innerText; break;
      }

      let isArticle = false;
      let articleTitle = '';
      const userNameEl = a.querySelector('div[data-testid="User-Name"]');
      const userNameText = userNameEl ? userNameEl.innerText : '';

      const sc = a.querySelector('[data-testid="socialContext"]');
      if (sc && /article/i.test(sc.innerText || '')) isArticle = true;
      if (!isArticle) {
        for (const el of a.querySelectorAll('span')) {
          const t = (el.innerText || '').trim();
          if (t === 'Article' || t === 'X Article' || t === 'Long Post') { isArticle = true; break; }
        }
      }

      const headings = a.querySelectorAll('h1, h2, h3, [role="heading"]');
      for (const h of headings) {
        if (quoteContainer && quoteContainer.contains(h)) continue;
        if (userNameEl && userNameEl.contains(h)) continue;
        const t = (h.innerText || '').trim();
        if (t && t.length > 5 && t.length < 300 && t !== userNameText.split('\n')[0]) {
          articleTitle = t;
          isArticle = true;
          break;
        }
      }
      if (!articleTitle) {
        let best = null, bestScore = 0;
        for (const el of a.querySelectorAll('span, div')) {
          if (el.children.length > 0) continue;
          if (quoteContainer && quoteContainer.contains(el)) continue;
          if (userNameEl && userNameEl.contains(el)) continue;
          const t = (el.innerText || '').trim();
          if (!t || t.length < 10 || t.length > 240) continue;
          if (/^@/.test(t) || /^From\s/i.test(t)) continue;
          const cs = window.getComputedStyle(el);
          const weight = parseInt(cs.fontWeight) || 400;
          const size = parseFloat(cs.fontSize) || 0;
          if (weight < 600 || size < 15) continue;  // only bold + larger-than-body
          const score = weight + size * 10;
          if (score > bestScore) { bestScore = score; best = t; }
        }
        if (best) { articleTitle = best; isArticle = true; }
      }

      if (isArticle && !text) {
        const paragraphs = [];
        for (const p of a.querySelectorAll('p, div[dir="auto"], span[dir="auto"]')) {
          if (quoteContainer && quoteContainer.contains(p)) continue;
          if (userNameEl && userNameEl.contains(p)) continue;
          const pt = (p.innerText || '').trim();
          if (!pt || pt.length < 25) continue;
          if (pt === articleTitle) continue;
          if (/^@/.test(pt)) continue;
          if (userNameText && pt === userNameText) continue;
          paragraphs.push(pt);
          if (paragraphs.join(' ').length > 500) break;
        }
        text = paragraphs.join('\n\n');
      }

      const userBlock = a.querySelector('div[data-testid="User-Name"]');
      const userTxt = textOrEmpty(userBlock);
      const authorLines = userTxt.split('\n').map(s => s.trim()).filter(Boolean);
      const authorName = authorLines[0] || '';
      const handle = (userTxt.match(/@[A-Za-z0-9_]+/) || [''])[0];

      const avEl = a.querySelector('img[src*="profile_images"]');
      const avatar = avEl ? avEl.getAttribute('src') : '';

      const media = [];
      const photos = a.querySelectorAll('div[data-testid="tweetPhoto"]');
      const seenUrls = new Set();
      for (const p of photos) {
        if (quoteContainer && quoteContainer.contains(p)) continue;
        const img = p.querySelector('img');
        if (img && img.src && !seenUrls.has(img.src)) {
          seenUrls.add(img.src);
          media.push({type: 'image', url: img.src});
        }
      }
      if (isArticle && media.length === 0) {
        for (const img of a.querySelectorAll('img')) {
          if (quoteContainer && quoteContainer.contains(img)) continue;
          const s = img.getAttribute('src') || '';
          if (!s || s.includes('profile_images') || s.includes('emoji') || s.includes('badge')) continue;
          if (!s.includes('twimg.com') && !s.includes('pbs.twimg.com')) continue;
          if (seenUrls.has(s)) continue;
          seenUrls.add(s);
          media.push({type: 'image', url: s});
          break;
        }
      }
      const videoEl = a.querySelector('[data-testid="videoPlayer"], [data-testid="videoComponent"]');
      if (videoEl && (!quoteContainer || !quoteContainer.contains(videoEl))) {
        const v = videoEl.querySelector('video');
        const poster = v ? v.getAttribute('poster') : '';
        const thumb = videoEl.querySelector('img[src*="ext_tw_video_thumb"], img[src*="amplify_video_thumb"], img');
        const url = poster || (thumb ? thumb.src : '');
        if (url) media.push({type: 'video', url});
      }

      let quoted = null;
      if (quoteContainer) {
        const qUser = quoteContainer.querySelector('div[data-testid="User-Name"]');
        const qUserTxt = textOrEmpty(qUser);
        const qLines = qUserTxt.split('\n').map(s => s.trim()).filter(Boolean);
        const qName = qLines[0] || '';
        const qHandle = (qUserTxt.match(/@[A-Za-z0-9_]+/) || [''])[0];
        const qTextEl = quoteContainer.querySelector('div[data-testid="tweetText"]');
        const qText = textOrEmpty(qTextEl);
        const qImg = quoteContainer.querySelector('img[src*="pbs.twimg.com/media"]');
        const qCover = qImg ? qImg.src : '';
        quoted = {name: qName, handle: qHandle, text: qText, cover: qCover};
      }

      let linkCard = null;
      for (const cw of a.querySelectorAll('[data-testid="card.wrapper"]')) {
        if (quoteContainer && quoteContainer.contains(cw)) continue;

        const link = cw.querySelector('a[href]');
        let cardUrl = link ? link.getAttribute('href') : '';
        if (cardUrl && cardUrl.startsWith('/')) cardUrl = 'https://x.com' + cardUrl;

        const texts = [];
        for (const el of cw.querySelectorAll('span, div')) {
          if (el.children.length > 0) continue;  // leaf only
          const t = (el.innerText || '').trim();
          if (!t) continue;
          texts.push(t);
        }
        const uniq = Array.from(new Set(texts));

        let domain = '', title = '', desc = '';
        for (const t of uniq) {
          const m = t.match(/^From\s+(.+)$/);
          if (m) { domain = m[1].trim(); break; }
        }
        let longest = '';
        for (const t of uniq) {
          if (/^From\s+/i.test(t)) continue;
          if (t.length < 8 || t.length > 240) continue;
          if (t.length > longest.length) longest = t;
        }
        title = longest;
        const remaining = uniq.filter(t => t !== title && !/^From\s+/i.test(t) && t.length > 20 && t.length < 240);
        desc = remaining.slice(0, 2).join(' ');

        if (!domain && cardUrl) {
          try { domain = new URL(cardUrl).hostname.replace(/^www\./, ''); } catch (e) {}
        }

        const imgEl = cw.querySelector('img');
        const cover = imgEl ? imgEl.getAttribute('src') : '';
        if (cardUrl || title || cover) {
          linkCard = {url: cardUrl, domain, title, description: desc, cover};
        }
        break;
      }

      let replyTo = '';
      for (const d of a.querySelectorAll('div')) {
        const t = (d.innerText || '').trim();
        if (t.startsWith('Replying to') && t.length < 300 && !t.includes('\n')) { replyTo = t; break; }
      }

      const timeEl = a.querySelector('time');
      const dt = timeEl ? timeEl.getAttribute('datetime') : '';

      out.push({tid, href, text, authorName, handle, avatar, media, quoted, linkCard, replyTo, dt, isArticle, articleTitle});
    } catch (e) {}
  }
  return out;
}
"""

    for step in range(max_scrolls):
        if step < 3:
            log(f"twitter: entering scroll {step}")
        try:
            rows = page.evaluate(extract_js)
        except Exception as e:
            log(f"twitter: extract failed at scroll {step}: {e}")
            rows = []
        n = len(rows)
        new_count = seen_known = 0

        for row in rows:
            tid = row.get("tid")
            if not tid or tid in seen:
                continue
            seen.add(tid)
            ext_id = f"twitter:{tid}"
            if ext_id in known:
                seen_known += 1
                if not full:
                    continue

            href = row.get("href", "")
            full_url = f"https://x.com{href}" if href.startswith("/") else href
            text = row.get("text", "") or ""
            created_ts = 0
            dt = row.get("dt")
            if dt:
                try:
                    created_ts = int(
                        datetime.fromisoformat(
                            dt.replace(
                                "Z", "+00:00")).timestamp())
                except Exception:
                    pass
            media = row.get("media") or []
            cover = ""
            for m in media:
                if m.get("url"):
                    cover = m["url"]
                    break

            avatar = row.get("avatar") or ""
            if avatar:
                for size in (
                    "_normal",
                    "_bigger",
                    "_mini",
                        "_reasonably_small"):
                    if size in avatar:
                        avatar = avatar.replace(size, "_400x400", 1)
                        break
            if row.get("isArticle") and row.get("articleTitle"):
                title = row["articleTitle"]
            elif text:
                title = text.splitlines()[0]
            else:
                title = f"Tweet by {row.get('handle') or 'unknown'}"
            if len(title) > 200:
                title = title[:200] + "…"
            collected.append(
                {
                    "id": ext_id, "source": "twitter", "url": full_url,
                    "title": title, "excerpt": text[:500],
                    "cover": cover,
                    "author": f"{row.get('authorName') or ''} {row.get('handle') or ''}".strip(),
                    "created_ts": created_ts,
                    "avatar": avatar,
                    "media": media,
                    "quoted": row.get("quoted"),
                    "link_card": row.get("linkCard"),
                    "reply_to": row.get("replyTo") or "",
                }
            )
            new_count += 1

        now_t = time.time()
        if step % 5 == 0 or (now_t - last_log) > 10:
            log(
                f"twitter: scroll={step} articles_in_dom={n} new_this_pass={new_count} seen_known={seen_known} total_new={
                    len(collected)}")
            last_log = now_t

        if limit and len(collected) >= limit:
            log(f"twitter: hit limit={limit} at scroll {step}")
            break

        if not full and new_count == 0 and seen_known > 0:
            repeat_hits += 1
            if repeat_hits >= 2:
                log(
                    f"twitter: caught up at scroll {step}, new={
                        len(collected)}")
                break
        else:
            repeat_hits = 0

        if new_count == 0 and seen_known == 0:
            stable += 1
            if stable >= 5:
                log(
                    f"twitter: stable ({stable} passes with no changes) — stopping at scroll {step}")
                break
        else:
            stable = 0

        _scroll(page)

    page.close()
    log(f"twitter: collected {len(collected)} new")
    return collected

LINKEDIN_ITEM_SELECTORS = [
    "div.feed-shared-update-v2",
    "[data-urn*='urn:li:activity']",
    "li.artdeco-list__item",
]

def scrape_linkedin(
        ctx: BrowserContext,
        full: bool = False,
        max_scrolls: int = 300,
        limit: int | None = None) -> list[dict]:
    known = existing_ids("linkedin")
    log(f"linkedin: starting (known={len(known)}, full={full})")

    page = ctx.new_page()
    page.goto(
        "https://www.linkedin.com/my-items/saved-posts/",
        wait_until="domcontentloaded")
    _pause(3.0, 4.5)

    if "login" in page.url.lower() or "signup" in page.url.lower():
        log("linkedin: not logged in — run --login first")
        page.close()
        return []

    seen: set[str] = set()
    collected: list[dict] = []
    repeat_hits = 0
    stable = 0

    extract_js = r"""
() => {
  const out = [];
  const nodes = document.querySelectorAll('main div[data-view-name][data-chameleon-result-urn]');
  for (const n of nodes) {
    try {
      const urn = n.getAttribute('data-chameleon-result-urn') || '';
      if (!urn.startsWith('urn:li:activity:')) continue;

      const postLink = n.querySelector('a[href*="/feed/update/"]');
      const postUrl = postLink ? postLink.href : `https://www.linkedin.com/feed/update/${urn}/`;

      let text = '';
      const textEl = n.querySelector('p[class*="content-summary"]');
      if (textEl) {
        text = (textEl.innerText || '').trim();
        text = text.replace(/\s*…\s*(see|show) more\s*$/i, '').trim();
      }

      let authorName = '';
      const nameEl = n.querySelector('.entity-result__content-actor span[dir="ltr"] span[aria-hidden="true"]');
      if (nameEl) authorName = nameEl.innerText.trim();

      let authorTitle = '';
      const titleEl = n.querySelector('.entity-result__content-actor div.t-14');
      if (titleEl) authorTitle = titleEl.innerText.trim().slice(0, 180);

      let authorProfile = '';
      const profileLink = n.querySelector('.entity-result__content-actor a[href*="/in/"]');
      if (profileLink) authorProfile = profileLink.href.split('?')[0];

      function pickBiggestFromImg(im) {
        if (!im) return '';
        const srcset = im.getAttribute('srcset') || im.getAttribute('data-srcset') || '';
        if (srcset) {
          let best = '', bestW = 0;
          for (const part of srcset.split(',')) {
            const p = part.trim();
            const m = p.match(/^(\S+)\s+(\d+)w$/);
            if (m) {
              const w = parseInt(m[2]);
              if (w > bestW) { bestW = w; best = m[1]; }
            }
          }
          if (best) return best;
        }
        return im.getAttribute('src') || '';
      }
      const avEl = n.querySelector('img.presence-entity__image, img[class*="EntityPhoto"]');
      const avatar = pickBiggestFromImg(avEl);

      let ageText = '';
      for (const p of n.querySelectorAll('.entity-result__content-actor p, .entity-result__content-actor span[aria-hidden="true"]')) {
        const t = (p.innerText || '').trim();
        const m = t.match(/^(\d+)(s|m|h|d|w|mo|y|yr)\b/i);
        if (m) { ageText = m[0].toLowerCase(); break; }
      }

      let cover = '';
      let bestImg = null, bestScore = 0;
      for (const im of n.querySelectorAll('img')) {
        const cls = im.className || '';
        if (/presence-entity|EntityPhoto|actor__avatar/.test(cls)) continue;
        const w = parseInt(im.getAttribute('width') || '0') || im.naturalWidth || 0;
        const score = w || 100;
        if (score > bestScore) { bestScore = score; bestImg = im; }
      }
      cover = pickBiggestFromImg(bestImg);

      let linkCard = null;
      const embedded = n.querySelector('a.entity-result__content-embedded-object, .entity-result__content-embedded-object');
      if (embedded) {
        const embUrl = (embedded.href || embedded.querySelector('a[href]') && embedded.querySelector('a[href]').href) || '';
        const lines = [];
        for (const el of embedded.querySelectorAll('span, div')) {
          if (el.children.length) continue;
          const t = (el.innerText || '').trim();
          if (t) lines.push(t);
        }
        const uniq = Array.from(new Set(lines));
        let lcTitle = '', lcDomain = '';
        for (const t of uniq) {
          if (t.length > 8 && t.length > lcTitle.length && t.length < 220) lcTitle = t;
        }
        for (const t of uniq) {
          if (t.length < 120 && /\w\.(com|org|net|ai|io|co|edu|gov|so|dev|app|tv|info|biz)\b/i.test(t) && t !== lcTitle) {
            lcDomain = t; break;
          }
        }
        const embImg = embedded.querySelector('img');
        const lcCover = embImg ? pickBiggestFromImg(embImg) : '';
        if (embUrl || lcTitle || lcCover) {
          linkCard = {url: embUrl, domain: lcDomain, title: lcTitle, description: '', cover: lcCover};
        }
      }

      out.push({urn, url: postUrl, text, authorName, authorTitle, authorProfile, avatar, cover, ageText, linkCard});
    } catch (e) {}
  }
  return out;
}
"""

    for step in range(max_scrolls):
        if step < 3:
            log(f"linkedin: entering scroll {step}")
        try:
            rows = page.evaluate(extract_js)
        except Exception as e:
            log(f"linkedin: extract failed at scroll {step}: {e}")
            rows = []
        n = len(rows)
        new_count = seen_known = 0

        for row in rows:
            urn = row.get("urn")
            if not urn or urn in seen:
                continue
            seen.add(urn)
            ext_id = f"linkedin:{urn}"
            if ext_id in known:
                seen_known += 1
                if not full:
                    continue

            url = row.get(
                "url") or f"https://www.linkedin.com/feed/update/{urn}/"
            text = (row.get("text") or "").strip()
            author_name = (row.get("authorName") or "").strip()
            author_title = (row.get("authorTitle") or "").strip()
            author_combined = f"{author_name} — {author_title}" if author_title else author_name

            title = text.splitlines()[0] if text else f"Post by {
                author_name or 'unknown'}"
            if len(title) > 200:
                title = title[:200] + "…"

            age_text = (row.get("ageText") or "").strip().lower()
            created_ts = 0
            m = re.match(r"^(\d+)(s|m|h|d|w|mo|y|yr)$", age_text)
            if m:
                n_units = int(m.group(1))
                unit = m.group(2)
                sec = {
                    "s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800,
                    "mo": 2592000, "y": 31536000, "yr": 31536000,
                }[unit]
                created_ts = int(time.time()) - n_units * sec

            link_card = row.get("linkCard")
            collected.append(
                {
                    "id": ext_id, "source": "linkedin", "url": url,
                    "title": title, "excerpt": text[:800],
                    "cover": row.get("cover") or (link_card.get("cover") if link_card else ""),
                    "author": author_combined,
                    "avatar": row.get("avatar") or "",
                    "created_ts": created_ts,
                    "media": [],
                    "quoted": None,
                    "link_card": link_card,
                    "reply_to": "",
                }
            )
            new_count += 1

        if limit and len(collected) >= limit:
            log(f"linkedin: hit limit={limit} at scroll {step}")
            break

        if not full and new_count == 0 and seen_known > 0:
            repeat_hits += 1
            if repeat_hits >= 2:
                log(f"linkedin: caught up at scroll {step}")
                break
        else:
            repeat_hits = 0

        if new_count == 0 and seen_known == 0:
            stable += 1
            if stable >= 5:
                log(f"linkedin: reached end at scroll {step}")
                break
        else:
            stable = 0

        _scroll(page)

    page.close()
    log(f"linkedin: collected {len(collected)} new")
    return collected

def do_login() -> None:
    PROFILE_DIR.mkdir(parents=True, exist_ok=True)
    print("Opening browser. Log in to x.com, linkedin.com, and youtube.com in their tabs,")
    print("then CLOSE THE WINDOW when done. The profile is saved to:")
    print(f"  {PROFILE_DIR}")
    with sync_playwright() as pw:
        ctx = pw.chromium.launch_persistent_context(
            str(PROFILE_DIR),
            headless=False,
            args=LAUNCH_ARGS,
            user_agent=USER_AGENT,
            viewport={"width": 1440, "height": 900},
            locale="en-US",
            timezone_id="America/New_York",
        )
        p1 = ctx.new_page()
        p1.goto("https://x.com/login")
        p2 = ctx.new_page()
        p2.goto("https://www.linkedin.com/login")
        p3 = ctx.new_page()
        p3.goto("https://www.youtube.com/playlist?list=WL")

        try:
            ctx.wait_for_event("close", timeout=0)
        except Exception:
            pass
    print("✓ profile saved")

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--login",
        action="store_true",
        help="open browser for manual login")
    ap.add_argument("--source", choices=["twitter", "linkedin"])
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--full", action="store_true")
    ap.add_argument(
        "--limit",
        type=int,
        help="stop after collecting N items (for quick tests)")
    ap.add_argument(
        "--headful",
        action="store_true",
        help="show the browser while scraping")
    args = ap.parse_args()

    ensure_schema()

    if args.login:
        do_login()
        return

    sources: list[str] = []
    if args.all:
        sources = ["twitter", "linkedin"]
    elif args.source:
        sources = [args.source]
    else:
        ap.print_help()
        sys.exit(1)

    pw, ctx = launch(headless=not args.headful)
    try:
        for src in sources:
            try:
                if src == "twitter":
                    items = scrape_twitter(
                        ctx, full=args.full, limit=args.limit)
                else:
                    items = scrape_linkedin(
                        ctx, full=args.full, limit=args.limit)
                n = upsert(items)
                log(f"{src}: upserted {n}")

                _pause(4.0, 7.0)
            except Exception as e:
                log(f"{src}: FAILED — {e}")
    finally:
        ctx.close()
        pw.stop()

if __name__ == "__main__":
    main()
