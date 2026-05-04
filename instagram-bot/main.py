import feedparser
import json
import os
import random
import re
from datetime import datetime
from openai import OpenAI

SEEN_FILE = "seen_stories.json"

RSS_FEEDS = [
    {
        "name": "Reuters Oddly Enough",
        "url": "https://feeds.reuters.com/reuters/oddlyEnoughNews",
    },
    {
        "name": "BBC News - World",
        "url": "http://feeds.bbci.co.uk/news/world/rss.xml",
    },
    {
        "name": "Mental Floss",
        "url": "https://www.mentalfloss.com/rss",
    },
    {
        "name": "Atlas Obscura",
        "url": "https://www.atlasobscura.com/feeds/latest",
    },
    {
        "name": "Science Daily - Strange & Offbeat",
        "url": "https://www.sciencedaily.com/rss/strange_offbeat.xml",
    },
    {
        "name": "The Guardian - Weird News",
        "url": "https://www.theguardian.com/news/series/newsblog/rss",
    },
    {
        "name": "New Scientist",
        "url": "https://www.newscientist.com/feed/home/",
    },
]


def load_seen_stories():
    if os.path.exists(SEEN_FILE):
        with open(SEEN_FILE, "r", encoding="utf-8") as f:
            return set(json.load(f))
    return set()


def save_seen_story(story_id):
    seen = load_seen_stories()
    seen.add(story_id)
    with open(SEEN_FILE, "w", encoding="utf-8") as f:
        json.dump(list(seen), f, ensure_ascii=False, indent=2)


def fetch_stories_from_feed(feed_info):
    print(f"  Fetching: {feed_info['name']} ...")
    try:
        feed = feedparser.parse(feed_info["url"])
        stories = []
        for entry in feed.entries:
            title = entry.get("title", "").strip()
            link = entry.get("link", "").strip()
            summary = entry.get("summary", entry.get("description", "")).strip()

            if not title or not link:
                continue

            story = {
                "id": link,
                "title": title,
                "link": link,
                "summary": summary,
                "source": feed_info["name"],
                "published": entry.get("published", ""),
            }
            stories.append(story)

        print(f"    Found {len(stories)} stories.")
        return stories
    except Exception as e:
        print(f"    ERROR fetching {feed_info['name']}: {e}")
        return []


def fetch_all_stories():
    all_stories = []
    for feed_info in RSS_FEEDS:
        stories = fetch_stories_from_feed(feed_info)
        all_stories.extend(stories)
    return all_stories


def pick_story(all_stories, seen_ids):
    unseen = [s for s in all_stories if s["id"] not in seen_ids]

    if not unseen:
        print("\nNo new unseen stories available. Resetting seen history...")
        unseen = all_stories

    if not unseen:
        print("No stories found at all. Check your internet connection or feed URLs.")
        return None

    chosen = random.choice(unseen)
    return chosen


def clean_html(text):
    clean = re.sub(r"<[^>]+>", "", text)
    clean = (
        clean.replace("&amp;", "&")
        .replace("&lt;", "<")
        .replace("&gt;", ">")
        .replace("&quot;", '"')
        .replace("&#39;", "'")
        .replace("&nbsp;", " ")
    )
    return clean.strip()


def generate_arabic_caption(story):
    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key:
        raise EnvironmentError("OPENAI_API_KEY is not set in environment variables.")

    client = OpenAI(api_key=api_key)

    title = clean_html(story["title"])
    summary = clean_html(story["summary"])
    source = story["source"]

    prompt = f"""أنت كاتب محتوى عربي محترف متخصص في إنشاء منشورات إنستغرام جذابة وفيروسية.

لديك الخبر التالي باللغة الإنجليزية:
العنوان: {title}
الملخص: {summary}
المصدر: {source}

مهمتك:
1. اكتب منشور إنستغرام بالعربية الفصحى المبسطة (يفهمها الجمهور العربي الواسع).
2. ابدأ بجملة افتتاحية مثيرة للاهتمام أو سؤال يشد القارئ فوراً.
3. لخص الخبر بأسلوب شيق وواضح في 3-5 جمل.
4. اختم بجملة تحفّز التفاعل (مثل رأيك؟ أو شاركنا تجربتك).
5. أضف في النهاية 10-15 هاشتاق عربي وإنجليزي مناسبة للخبر.

المنشور يجب أن يكون:
- جذاباً وفيروسياً
- واضحاً وسهل القراءة
- يثير الفضول والتفاعل
- مناسب لجمهور عربي على إنستغرام

اكتب المنشور مباشرة بدون أي مقدمة أو تعليق منك."""

    print("\n[Step 4] Sending story to GPT-4o for Arabic caption generation...")

    response = client.chat.completions.create(
        model="gpt-4o",
        messages=[
            {
                "role": "system",
                "content": "أنت كاتب محتوى عربي محترف ومتخصص في إنشاء محتوى فيروسي لمنصات التواصل الاجتماعي.",
            },
            {"role": "user", "content": prompt},
        ],
        temperature=0.85,
        max_tokens=800,
    )

    caption = response.choices[0].message.content.strip()
    return caption


def main():
    print("=" * 60)
    print("  Instagram Bot - News Scraper + Arabic Caption")
    print(f"  Run at: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 60)

    print("\n[Step 1] Loading seen stories history...")
    seen_ids = load_seen_stories()
    print(f"  Previously seen: {len(seen_ids)} stories.")

    print("\n[Step 2] Fetching stories from RSS feeds...")
    all_stories = fetch_all_stories()
    print(f"\n  Total stories fetched: {len(all_stories)}")

    print("\n[Step 3] Picking a new story...")
    story = pick_story(all_stories, seen_ids)

    if not story:
        return

    title = clean_html(story["title"])
    summary = clean_html(story["summary"])

    print("\n" + "=" * 60)
    print("  SELECTED STORY (English)")
    print("=" * 60)
    print(f"  Source  : {story['source']}")
    print(f"  Title   : {title}")
    print(f"  Link    : {story['link']}")
    print(f"  Summary : {summary[:300]}{'...' if len(summary) > 300 else ''}")
    print("=" * 60)

    arabic_caption = generate_arabic_caption(story)

    print("\n" + "=" * 60)
    print("  GENERATED ARABIC CAPTION")
    print("=" * 60)
    print(arabic_caption)
    print("=" * 60)

    save_seen_story(story["id"])
    print("\n[Done] Story saved to seen history. Next run will skip this story.")

    return {
        "story": story,
        "arabic_caption": arabic_caption,
    }


if __name__ == "__main__":
    main()
