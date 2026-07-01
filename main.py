import os
import re
import json
import time
import random
import subprocess
from threading import Thread

import feedparser
import requests
import schedule
from flask import Flask
from openai import OpenAI

# --- المفاتيح من بيئة ريبليت ---
ACCESS_TOKEN = os.environ.get('IG_ACCESS_TOKEN')
INSTAGRAM_ACCOUNT_ID = os.environ.get('IG_ACCOUNT_ID')
OPENAI_API_KEY = os.environ.get('OPENAI_API_KEY')

SEEN_FILE = "seen_stories.json"
TEMP_IMAGE = "temp_image.jpg"
TEMP_AUDIO = "temp_audio.mp3"
TEMP_VIDEO = "output_reel.mp4"

client = OpenAI(api_key=OPENAI_API_KEY)
app = Flask(__name__)

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


@app.route('/')
def home():
    """هذه الصفحة تبقي السيرفر مستيقظاً"""
    return "Radar News Bot is Alive and Running!"


def run_server():
    app.run(host='0.0.0.0', port=5000)


# --- ذاكرة القصص المنشورة سابقاً (لتفادي تكرار نفس الخبر) ---
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


# --- جلب الأخبار من مصادر RSS واختيار قصة جديدة ---
def fetch_stories_from_feed(feed_info):
    print(f"  جلب: {feed_info['name']} ...")
    try:
        feed = feedparser.parse(feed_info["url"])
        stories = []
        for entry in feed.entries:
            title = entry.get("title", "").strip()
            link = entry.get("link", "").strip()
            summary = entry.get("summary", entry.get("description", "")).strip()

            if not title or not link:
                continue

            stories.append({
                "id": link,
                "title": title,
                "link": link,
                "summary": summary,
                "source": feed_info["name"],
                "published": entry.get("published", ""),
            })

        print(f"    تم العثور على {len(stories)} خبر.")
        return stories
    except Exception as e:
        print(f"    ❌ خطأ في جلب {feed_info['name']}: {e}")
        return []


def fetch_all_stories():
    all_stories = []
    for feed_info in RSS_FEEDS:
        all_stories.extend(fetch_stories_from_feed(feed_info))
    return all_stories


def pick_story(all_stories, seen_ids):
    unseen = [s for s in all_stories if s["id"] not in seen_ids]

    if not unseen:
        print("  لا توجد قصص جديدة غير منشورة، سيتم إعادة تدوير القصص القديمة...")
        unseen = all_stories

    if not unseen:
        print("  ❌ لم يتم العثور على أي قصص إطلاقاً.")
        return None

    return random.choice(unseen)


# --- توليد المحتوى العربي (كابشن + سيناريو صوتي) بالاعتماد على خبر حقيقي ---
def generate_arabic_content(story):
    print("🧠 جاري كتابة السيناريو العربي بالاعتماد على الخبر...")
    title = clean_html(story["title"])
    summary = clean_html(story["summary"])
    source = story["source"]

    text_prompt = f"""
    أنت صانع محتوى محترف تدير حساب انستقرام عراقي اسمه "رادار نيوز"، متخصص بتحويل الأخبار العالمية الغريبة والمثيرة إلى ريلز عربية جذابة.

    هذا خبر حقيقي من مصدر موثوق، اعتمد عليه فقط ولا تخترع معلومات غير موجودة فيه:
    العنوان: {title}
    الملخص: {summary}
    المصدر: {source}

    اكتب المحتوى بالعربية الفصحى المبسطة والسليمة 100% (لتجنب أخطاء نطق الراوي الآلي).

    استخدم هذا التنسيق بالضبط في ردك:
    الملخص: (كلمتين إلى ثلاث كلمات تلخص جوهر الخبر بصرياً)
    الصوت: (سيناريو تعليق صوتي بحدود 40 إلى 50 كلمة، يبدأ بـ Hook قوي جداً يشد المستمع من الثانية الأولى)
    الكابشن: (منشور انستغرام: جملة افتتاحية مثيرة + تلخيص شيق للخبر في 3-5 جمل + دعوة للتفاعل + 10-15 هاشتاق عربي وإنجليزي مناسب)
    """

    try:
        response = client.chat.completions.create(
            model="gpt-4o",
            messages=[{"role": "user", "content": text_prompt}],
            temperature=0.8,
        )
        full_text = response.choices[0].message.content.strip()

        topic_summary = title
        voice_script = f"هل تعلم أن {title}؟"
        caption = full_text

        for line in full_text.split('\n'):
            if line.startswith("الملخص:"):
                topic_summary = line.replace("الملخص:", "").replace("(", "").replace(")", "").strip()
            elif line.startswith("الصوت:"):
                voice_script = line.replace("الصوت:", "").replace("(", "").replace(")", "").strip()

        if "الكابشن:" in full_text:
            caption = full_text.split("الكابشن:")[1].strip()

        print(f"✅ تم كتابة السيناريو: {topic_summary}")
        return topic_summary, voice_script, caption
    except Exception as e:
        print(f"❌ خطأ في توليد المحتوى: {e}")
        return None, None, None


# --- توليد صورة غلاف عمودية (9:16) آمنة السياسة عبر DALL-E 3 ---
def build_image_prompt(story, topic_summary):
    title = clean_html(story["title"])
    summary = clean_html(story["summary"])

    response = client.chat.completions.create(
        model="gpt-4o",
        messages=[
            {
                "role": "system",
                "content": (
                    "You are an expert at writing DALL-E 3 image prompts for vertical Instagram Reels covers. "
                    "Your prompts are vivid, visual, and always policy-safe. "
                    "Never include violence, blood, real people, political figures, "
                    "logos, or copyrighted elements. Focus on scene, atmosphere, and concept."
                ),
            },
            {
                "role": "user",
                "content": (
                    f"Write a single DALL-E 3 image prompt for a vertical (9:16) Instagram Reel cover about this news story.\n\n"
                    f"Core concept: {topic_summary}\n"
                    f"Title: {title}\n"
                    f"Summary: {summary[:500]}\n\n"
                    f"Requirements:\n"
                    f"- Highly conceptual, surreal, cinematic illustration\n"
                    f"- Vertical composition (9:16), no text\n"
                    f"- Masterpiece quality, highly detailed, cinematic lighting\n"
                    f"- No real people, no logos, no copyrighted elements\n"
                    f"- Output the prompt only, no explanation"
                ),
            },
        ],
        temperature=0.7,
        max_tokens=200,
    )

    return response.choices[0].message.content.strip()


def generate_cover_image(story, topic_summary):
    print("🎨 جاري بناء برومبت آمن للصورة...")
    try:
        image_prompt = build_image_prompt(story, topic_summary)
    except Exception as e:
        print(f"❌ تعذر بناء برومبت الصورة: {e}")
        return False

    print("🎨 جاري رسم اللوحة السريالية...")
    try:
        image_response = client.images.generate(
            model="dall-e-3",
            prompt=image_prompt,
            size="1024x1792",
            quality="hd",
            n=1,
        )
        image_url = image_response.data[0].url
        img_data = requests.get(image_url).content
        with open(TEMP_IMAGE, "wb") as handler:
            handler.write(img_data)
        return True
    except Exception as e:
        error_msg = str(e)
        if "content_policy_violation" in error_msg or "safety system" in error_msg.lower():
            print(f"⚠️ رفض DALL-E توليد هذه الصورة بسبب سياسة المحتوى: {error_msg[:200]}")
        else:
            print(f"❌ خطأ في توليد الصورة: {error_msg[:200]}")
        return False


def generate_voice_over(text):
    print("🎙️ جاري تسجيل الصوت بجودة HD بالفصحى...")
    try:
        response = client.audio.speech.create(
            model="tts-1-hd",
            voice="onyx",
            input=text,
        )
        with open(TEMP_AUDIO, "wb") as f:
            f.write(response.content)
        return True
    except Exception as e:
        print(f"❌ خطأ في توليد الصوت: {e}")
        return False


def create_video_reel():
    print("🎬 جاري المونتاج وإضافة الحركة البصرية...")
    try:
        command = [
            "ffmpeg", "-y", "-loop", "1", "-framerate", "30", "-i", TEMP_IMAGE, "-i", TEMP_AUDIO,
            "-vf", "zoompan=z='min(zoom+0.0005,1.15)':x='iw/2-(iw/zoom)/2':y='ih/2-(ih/zoom)/2':d=1500:s=1024x1792:fps=30",
            "-c:v", "libx264", "-c:a", "aac", "-b:a", "192k",
            "-pix_fmt", "yuv420p", "-shortest", TEMP_VIDEO,
        ]
        subprocess.run(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)
        return True
    except Exception as e:
        print(f"❌ خطأ في المونتاج: {e}")
        return False


def upload_to_temp_server():
    print("🌐 جاري الرفع للسيرفر المؤقت (tmpfiles.org)...")
    try:
        with open(TEMP_VIDEO, "rb") as f:
            res = requests.post("https://tmpfiles.org/api/v1/upload", files={"file": f}).json()
        return res['data']['url'].replace("tmpfiles.org/", "tmpfiles.org/dl/")
    except Exception as e:
        print(f"❌ خطأ في الرفع: {e}")
        return None


def post_reel_to_instagram(video_url, caption):
    print("🚀 جاري إرسال الريلز لميتا ومعالجته...")
    url = f"https://graph.facebook.com/v25.0/{INSTAGRAM_ACCOUNT_ID}/media"

    params = {'video_url': video_url, 'caption': caption, 'media_type': 'REELS', 'access_token': ACCESS_TOKEN}
    res = requests.post(url, data=params).json()

    if 'id' in res:
        creation_id = res['id']
        status_url = f"https://graph.facebook.com/v25.0/{creation_id}?fields=status_code&access_token={ACCESS_TOKEN}"

        is_ready = False
        for _ in range(15):
            time.sleep(10)
            status_res = requests.get(status_url).json()
            if status_res.get('status_code') == 'FINISHED':
                is_ready = True
                break

        if is_ready:
            print("🚀 جاري النشر للإكسبلور...")
            publish_url = f"https://graph.facebook.com/v25.0/{INSTAGRAM_ACCOUNT_ID}/media_publish"
            publish_res = requests.post(publish_url, data={'creation_id': creation_id, 'access_token': ACCESS_TOKEN}).json()

            if 'id' in publish_res:
                print("🎉 تم النشر بنجاح!")
                return True
    return False


def cleanup_temp_files():
    for path in (TEMP_IMAGE, TEMP_AUDIO, TEMP_VIDEO):
        if os.path.exists(path):
            os.remove(path)


def job():
    """دورة الإنتاج الآلية: جلب خبر حقيقي -> محتوى عربي -> صورة -> صوت -> فيديو -> نشر"""
    print("⏳ حان وقت النشر المجدول! بدء دورة الإنتاج...")

    seen_ids = load_seen_stories()

    print("📡 جاري جلب الأخبار من مصادر RSS...")
    all_stories = fetch_all_stories()
    if not all_stories:
        print("❌ لم يتم العثور على أي أخبار. إلغاء الدورة.")
        return

    story = pick_story(all_stories, seen_ids)
    if not story:
        print("❌ لا توجد قصة صالحة للنشر. إلغاء الدورة.")
        return

    print(f"📰 القصة المختارة: {story['title']} ({story['source']})")

    topic_summary, voice_script, caption = generate_arabic_content(story)
    if not (topic_summary and voice_script and caption):
        print("❌ فشل توليد المحتوى العربي. إلغاء الدورة.")
        return

    if not generate_cover_image(story, topic_summary):
        print("❌ فشل توليد الصورة. إلغاء الدورة.")
        return

    if not generate_voice_over(voice_script):
        print("❌ فشل توليد الصوت. إلغاء الدورة.")
        cleanup_temp_files()
        return

    if not create_video_reel():
        print("❌ فشل تركيب الفيديو. إلغاء الدورة.")
        cleanup_temp_files()
        return

    public_video_url = upload_to_temp_server()
    if not public_video_url:
        print("❌ فشل الرفع للسيرفر المؤقت. إلغاء الدورة.")
        cleanup_temp_files()
        return

    if post_reel_to_instagram(public_video_url, caption):
        save_seen_story(story["id"])

    cleanup_temp_files()


# --- نقطة انطلاق النظام الآلي ---
if __name__ == "__main__":
    # تشغيل سيرفر النبض في الخلفية
    server_thread = Thread(target=run_server)
    server_thread.daemon = True
    server_thread.start()

    # جدولة النشر (التوقيت بـ UTC - سيرفر ريبليت)
    # 11:00 UTC = 2:00 PM توقيت محلي
    # 17:00 UTC = 8:00 PM توقيت محلي
    schedule.every().day.at("11:00").do(job)
    schedule.every().day.at("17:00").do(job)

    print("✅ النظام الآلي يعمل الآن في الخلفية... سيتم النشر في الأوقات المحددة.")
    print("🌐 سيرفر النبض يعمل، يمكنك الآن ربطه بـ UptimeRobot.")

    # حلقة لانهائية لتبقي السكربت يراقب الوقت
    while True:
        schedule.run_pending()
        time.sleep(60)
