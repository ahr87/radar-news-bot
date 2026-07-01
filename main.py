import os
import re
import json
import time
import random
import base64
import shutil
import subprocess
from threading import Thread

import feedparser
import requests
import schedule
from flask import Flask
from openai import OpenAI
from PIL import Image, ImageDraw, ImageFont, ImageEnhance

# --- المفاتيح من بيئة ريبليت ---
ACCESS_TOKEN = os.environ.get('IG_ACCESS_TOKEN')
INSTAGRAM_ACCOUNT_ID = os.environ.get('IG_ACCOUNT_ID')
OPENAI_API_KEY = os.environ.get('OPENAI_API_KEY')
# اختياريان: إذا تم ضبطهما، يُستخدم صوت ElevenLabs الواقعي بدل صوت OpenAI
ELEVENLABS_API_KEY = os.environ.get('ELEVENLABS_API_KEY')
ELEVENLABS_VOICE_ID = os.environ.get('ELEVENLABS_VOICE_ID')

SEEN_FILE = "seen_stories.json"
TEMP_IMAGE = "temp_image.jpg"
TEMP_AUDIO = "temp_audio.mp3"
TEMP_BASE_VIDEO = "base_reel.mp4"
TEMP_VIDEO = "output_reel.mp4"
CAPTION_DIR = "captions_tmp"

VIDEO_WIDTH = 1024
VIDEO_HEIGHT = 1792

# قالب العلامة التجارية الثابت (يُطبَّق برمجياً على كل صورة، بدل الاعتماد فقط على طلب GPT)
BRAND_NAME = "RADAR NEWS"
BRAND_ACCENT_COLOR = (255, 196, 60)
BRAND_FRAME_WIDTH = 14
ASSETS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "assets", "fonts")
BRAND_FONT_PATH = os.path.join(ASSETS_DIR, "LiberationSans-Bold.ttf")
CAPTION_FONT_PATH = os.path.join(ASSETS_DIR, "NotoNaskhArabic-Bold.ttf")

client = OpenAI(api_key=OPENAI_API_KEY)
app = Flask(__name__)

RSS_FEEDS = [
    {
        "name": "Atlas Obscura",
        "url": "https://www.atlasobscura.com/feeds/latest",
    },
    {
        "name": "Science Daily - Strange & Offbeat",
        "url": "https://www.sciencedaily.com/rss/strange_offbeat.xml",
    },
    {
        "name": "New Scientist",
        "url": "https://www.newscientist.com/feed/home/",
    },
    {
        "name": "IFLScience",
        "url": "https://www.iflscience.com/rss",
    },
    {
        "name": "Live Science",
        "url": "https://www.livescience.com/feeds/all",
    },
    {
        "name": "Smithsonian Magazine - Smart News",
        "url": "https://www.smithsonianmag.com/rss/smart-news/",
    },
    {
        "name": "Ancient Origins",
        "url": "https://www.ancient-origins.net/rss.xml",
    },
]

# هوية بصرية ثابتة تُضاف لكل صورة غلاف، عشان يكون فيه "تيمبلت" مرئي موحّد يميّز صفحة "رادار نيوز"
BRAND_VISUAL_STYLE = (
    "signature visual identity: vibrant electric-blue and warm gold color palette, "
    "bright glowing highlights, rich saturated colors, high contrast, well-lit scene "
    "(never dark, dim, muddy, or low-key), energetic dreamlike surreal digital-art style, "
    "consistent with an eye-catching curiosity/discovery brand"
)


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
def _extract_field(full_text, field_name):
    for line in full_text.split('\n'):
        if line.strip().startswith(field_name):
            return line.split(":", 1)[1].strip() if ":" in line else ""
    return ""


def generate_arabic_content(story):
    print("🧠 جاري كتابة السيناريو العربي بالاعتماد على الخبر...")
    title = clean_html(story["title"])
    summary = clean_html(story["summary"])
    source = story["source"]

    text_prompt = f"""
    أنت صانع محتوى محترف تدير حساب انستقرام عراقي اسمه "رادار نيوز"، متخصص بصناعة ريلز عربية فضولية شديدة الجذب مبنية على أخبار حقيقية، بهدف الوصول لصفحة الاكتشاف (الإكسبلور).

    هذا خبر حقيقي من مصدر موثوق:
    العنوان: {title}
    الملخص: {summary}
    المصدر: {source}

    القواعد المهمة:
    1. اعتمد فقط على المعلومات الموجودة فعلاً بالخبر أعلاه، ولا تخترع أي حقيقة غير موجودة فيه.
    2. لا تُعِد صياغة العنوان الرئيسي المعروف للخبر. ابحث داخل الملخص عن أغرب وأندر تفصيل موجود فيه (رقم صادم، سبب غير متوقع، تفصيل جانبي قليل من ينتبه له)، واجعله محور المحتوى بدل الفكرة العامة المتوقعة.
    3. اكتب "فجوة فضول" حقيقية بالخطاف: ابدأ بسؤال أو جملة تخلق تشويقاً وتؤجل الكشف عن الإجابة/المفارقة لبعد سطر أو سطرين، بحيث يضطر القارئ يكمل القراءة أو المشاهدة عشان يعرف الجواب.
    4. اختم بسؤال حقيقي يحفّز الناس يكتبون تعليق (مو مجرد "شنو رأيك" عام، خليه سؤال مرتبط تحديداً بتفصيل الخبر).
    5. اكتب بالعربية الفصحى المبسطة والسليمة 100% (لتجنب أخطاء نطق الراوي الآلي، بدون كلمات إنجليزية أو رموز يصعب نطقها).

    استخدم هذا التنسيق بالضبط في ردك (بدون أي نص إضافي خارج هذا التنسيق):
    الملخص: (كلمتين إلى ثلاث كلمات تلخص المشهد البصري الأنسب للتفصيل النادر، لتوليد صورة عنه)
    الصوت: (سيناريو تعليق صوتي بحدود 40 إلى 55 كلمة: خطاف قوي بفجوة فضول + كشف تدريجي للتفصيل النادر)
    الخطاف: (أول جملة بالمنشور، مطابقة لروح سيناريو الصوت، سؤال أو جملة صادمة قصيرة)
    الجسم: (3 إلى 4 جمل تكشف التفصيل النادر تدريجياً بأسلوب قصصي شيق)
    السؤال الختامي: (سؤال قصير مرتبط تحديداً بالخبر يحفّز التعليقات)
    الهاشتاقات: (8 إلى 10 هاشتاقات عربية وإنجليزية مرتبطة تحديداً بموضوع الخبر، بدون هاشتاقات عامة مكررة)
    """

    try:
        response = client.chat.completions.create(
            model="gpt-4o",
            messages=[{"role": "user", "content": text_prompt}],
            temperature=0.9,
        )
        full_text = response.choices[0].message.content.strip()

        topic_summary = _extract_field(full_text, "الملخص") or title
        voice_script = _extract_field(full_text, "الصوت") or f"هل تعلم أن {title}؟"
        hook = _extract_field(full_text, "الخطاف") or topic_summary
        body = _extract_field(full_text, "الجسم") or summary[:300]
        closing_question = _extract_field(full_text, "السؤال الختامي") or "شنو رأيكم؟ 👇"
        hashtags = _extract_field(full_text, "الهاشتاقات") or "#رادار_نيوز #RadarNews"

        # تيمبلت ثابت لصفحة "رادار نيوز" — نفس البنية تتكرر بكل منشور لبناء هوية مميزة للصفحة
        caption = (
            f"{hook}\n\n"
            f"{body}\n\n"
            f"💬 {closing_question}\n"
            f"🔁 شير المنشور لصديق يحب الحقائق الغريبة\n\n"
            f"{hashtags} #رادار_نيوز #RadarNews"
        )

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
                    "Your prompts are vivid, visual, and always policy-safe, written entirely in English. "
                    "Never include violence, blood, real people, political figures, "
                    "logos, or copyrighted elements. Focus on scene, atmosphere, and concept. "
                    "Never ask the model to render any text, letters, numbers, signs, or writing in the "
                    "image, in any language or script — describe pure visuals only."
                ),
            },
            {
                "role": "user",
                "content": (
                    f"Write a single DALL-E 3 image prompt for a vertical (9:16) Instagram Reel cover about this news story. "
                    f"Translate the concept below into a purely visual English scene description — do not quote or embed "
                    f"the original Arabic text anywhere in the prompt.\n\n"
                    f"Core concept (Arabic, translate to a visual scene only): {topic_summary}\n"
                    f"Title: {title}\n"
                    f"Summary: {summary[:500]}\n\n"
                    f"Requirements:\n"
                    f"- Highly conceptual, surreal, cinematic illustration\n"
                    f"- Vertical composition (9:16)\n"
                    f"- ABSOLUTELY NO text, letters, numbers, symbols, signage, or writing anywhere in the image, "
                    f"in any language — this is critical, image models often render garbled text so avoid it entirely\n"
                    f"- Do NOT represent abstract ideas (math, science, data, language, time) using floating digits, "
                    f"equations, glyphs, or symbol clusters, even as decorative or stylized elements — express the "
                    f"concept only through concrete objects, creatures, scenery, colors, and lighting\n"
                    f"- Prefer a well-lit, glowing, or sunlit setting; avoid deep darkness, black voids, or dim night "
                    f"scenes unless absolutely essential to the concept\n"
                    f"- Masterpiece quality, highly detailed, cinematic lighting\n"
                    f"- No real people, no logos, no copyrighted elements\n"
                    f"- Apply this {BRAND_VISUAL_STYLE}\n"
                    f"- Output the prompt only, no explanation"
                ),
            },
        ],
        temperature=0.7,
        max_tokens=220,
    )

    return response.choices[0].message.content.strip()


def _save_generated_image(image_response):
    data = image_response.data[0]
    if getattr(data, "url", None):
        img_data = requests.get(data.url).content
    else:
        img_data = base64.b64decode(data.b64_json)
    with open(TEMP_IMAGE, "wb") as handler:
        handler.write(img_data)
    apply_brand_template(TEMP_IMAGE)


def apply_brand_template(image_path):
    """يطبّق هوية بصرية ثابتة (تفتيح + إطار + شعار) على كل صورة، بغض النظر عن ناتج الذكاء الاصطناعي."""
    try:
        img = Image.open(image_path).convert("RGB")

        # تصحيح تلقائي للسطوع والتباين والتشبع، لتفادي الصور الداكنة/الباهتة بشكل مضمون
        img = ImageEnhance.Brightness(img).enhance(1.15)
        img = ImageEnhance.Contrast(img).enhance(1.10)
        img = ImageEnhance.Color(img).enhance(1.25)

        img = img.convert("RGBA")
        width, height = img.size

        # تدرّج داكن أسفل الصورة لوضوح الشعار
        gradient_height = int(height * 0.20)
        gradient = Image.new("RGBA", (width, gradient_height), (0, 0, 0, 0))
        gradient_draw = ImageDraw.Draw(gradient)
        for y in range(gradient_height):
            alpha = int(180 * (y / gradient_height))
            gradient_draw.line([(0, y), (width, y)], fill=(0, 0, 0, alpha))
        img.alpha_composite(gradient, (0, height - gradient_height))

        draw = ImageDraw.Draw(img)

        # إطار ثابت بلون العلامة التجارية يميّز شكل منشورات الصفحة
        draw.rectangle(
            [0, 0, width - 1, height - 1],
            outline=BRAND_ACCENT_COLOR + (255,),
            width=BRAND_FRAME_WIDTH,
        )

        # شعار نصي إنكليزي ثابت (باللاتيني عمداً لتفادي تشوّه الحروف العربية بالصور)
        font_size = max(30, width // 16)
        font = ImageFont.truetype(BRAND_FONT_PATH, font_size)
        margin = BRAND_FRAME_WIDTH + 26
        draw.text(
            (margin, height - margin - font_size),
            BRAND_NAME,
            font=font,
            fill=(255, 255, 255, 255),
        )

        img.convert("RGB").save(image_path, "JPEG", quality=95)
    except Exception as e:
        print(f"⚠️ تعذر تطبيق قالب العلامة على الصورة (سيتم المتابعة بدونه): {str(e)[:200]}")


def generate_cover_image(story, topic_summary):
    print("🎨 جاري بناء برومبت آمن للصورة...")
    try:
        image_prompt = build_image_prompt(story, topic_summary)
    except Exception as e:
        print(f"❌ تعذر بناء برومبت الصورة: {e}")
        return False

    print("🎨 جاري رسم اللوحة السريالية (dall-e-3)...")
    try:
        image_response = client.images.generate(
            model="dall-e-3",
            prompt=image_prompt,
            size="1024x1792",
            quality="hd",
            n=1,
        )
        _save_generated_image(image_response)
        return True
    except Exception as e:
        error_msg = str(e)
        if "content_policy_violation" in error_msg or "safety system" in error_msg.lower():
            print(f"⚠️ رفض DALL-E توليد هذه الصورة بسبب سياسة المحتوى: {error_msg[:200]}")
            return False
        print(f"⚠️ تعذر استخدام dall-e-3 ({error_msg[:150]})، جاري المحاولة عبر gpt-image-1...")

    try:
        image_response = client.images.generate(
            model="gpt-image-1",
            prompt=image_prompt,
            size="1024x1536",
            quality="high",
            n=1,
        )
        _save_generated_image(image_response)
        return True
    except Exception as e:
        error_msg = str(e)
        if "content_policy_violation" in error_msg or "safety system" in error_msg.lower():
            print(f"⚠️ رفض gpt-image-1 توليد هذه الصورة بسبب سياسة المحتوى: {error_msg[:200]}")
        else:
            print(f"❌ خطأ في توليد الصورة عبر gpt-image-1: {error_msg[:200]}")
        return False


def _generate_voice_elevenlabs(text):
    try:
        response = requests.post(
            f"https://api.elevenlabs.io/v1/text-to-speech/{ELEVENLABS_VOICE_ID}",
            headers={
                "xi-api-key": ELEVENLABS_API_KEY,
                "Content-Type": "application/json",
                "Accept": "audio/mpeg",
            },
            json={
                "text": text,
                "model_id": "eleven_multilingual_v2",
                "voice_settings": {
                    "stability": 0.5,
                    "similarity_boost": 0.8,
                    "style": 0.4,
                    "use_speaker_boost": True,
                },
            },
            timeout=60,
        )
        if response.status_code == 200 and response.content:
            with open(TEMP_AUDIO, "wb") as f:
                f.write(response.content)
            return True
        print(f"⚠️ فشل ElevenLabs (HTTP {response.status_code}): {response.text[:200]}")
        return False
    except Exception as e:
        print(f"⚠️ خطأ في الاتصال بـ ElevenLabs: {str(e)[:200]}")
        return False


def generate_voice_over(text):
    if ELEVENLABS_API_KEY and ELEVENLABS_VOICE_ID:
        print("🎙️ جاري تسجيل صوت واقعي عبر ElevenLabs...")
        if _generate_voice_elevenlabs(text):
            return True
        print("⚠️ سيتم التراجع لصوت OpenAI...")

    print("🎙️ جاري تسجيل صوت راوٍ طبيعي بالفصحى (OpenAI)...")
    try:
        response = client.audio.speech.create(
            model="gpt-4o-mini-tts",
            voice="coral",
            input=text,
            instructions=(
                "تحدث بالعربية الفصحى المبسطة كراوٍ عربي محترف بأسلوب وثائقي دافئ وواثق ومشوّق، "
                "بسرعة معتدلة، مع وقفات طبيعية قصيرة بين الجمل، ونبرة فضول تشد المستمع من أول كلمة، "
                "بدون أي رتابة أو نبرة آلية."
            ),
        )
        with open(TEMP_AUDIO, "wb") as f:
            f.write(response.content)
        return True
    except Exception as e:
        print(f"⚠️ تعذر استخدام gpt-4o-mini-tts ({str(e)[:150]})، جاري المحاولة عبر tts-1-hd...")

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
        print(f"❌ خطأ في توليد الصوت: {str(e)[:200]}")
        return False


def _transcribe_word_timestamps():
    with open(TEMP_AUDIO, "rb") as f:
        transcript = client.audio.transcriptions.create(
            model="whisper-1",
            file=f,
            language="ar",
            response_format="verbose_json",
            timestamp_granularities=["word"],
        )
    return [{"word": w.word, "start": w.start, "end": w.end} for w in (transcript.words or [])]


def _group_words_into_chunks(words, words_per_chunk=3):
    chunks = []
    for i in range(0, len(words), words_per_chunk):
        group = words[i:i + words_per_chunk]
        if not group:
            continue
        text = " ".join(w["word"].strip() for w in group)
        chunks.append({"text": text, "start": group[0]["start"], "end": group[-1]["end"]})
    return chunks


def _render_caption_image(text):
    band_height = 340
    img = Image.new("RGBA", (VIDEO_WIDTH, band_height), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    font = ImageFont.truetype(CAPTION_FONT_PATH, 62)

    bbox = draw.textbbox((0, 0), text, font=font, direction="rtl", language="ar")
    text_w, text_h = bbox[2] - bbox[0], bbox[3] - bbox[1]
    pad_x, pad_y = 40, 24
    box_w = min(VIDEO_WIDTH - 60, text_w + pad_x * 2)
    box_x0 = (VIDEO_WIDTH - box_w) // 2
    box_y0 = (band_height - text_h) // 2 - pad_y
    box_y1 = box_y0 + text_h + pad_y * 2

    draw.rounded_rectangle([box_x0, box_y0, box_x0 + box_w, box_y1], radius=20, fill=(0, 0, 0, 165))
    draw.text(
        (VIDEO_WIDTH // 2, (box_y0 + box_y1) // 2),
        text,
        font=font,
        fill=(255, 255, 255, 255),
        direction="rtl",
        language="ar",
        anchor="mm",
        align="center",
    )
    return img


def _burn_captions(base_video_path):
    """يحرق ترجمة عربية متزامنة مع الصوت داخل الفيديو (لأن أغلب المشاهدين يتفرجون بدون صوت)."""
    try:
        words = _transcribe_word_timestamps()
        if not words:
            print("⚠️ لم يتم استخراج توقيت للكلمات، سيُنشر الفيديو بدون ترجمة.")
            return False

        chunks = _group_words_into_chunks(words, words_per_chunk=3)
        if not chunks:
            return False

        os.makedirs(CAPTION_DIR, exist_ok=True)
        caption_paths = []
        for idx, chunk in enumerate(chunks):
            img = _render_caption_image(chunk["text"])
            path = os.path.join(CAPTION_DIR, f"caption_{idx}.png")
            img.save(path)
            caption_paths.append((path, chunk["start"], chunk["end"]))

        inputs = ["-i", base_video_path]
        for path, _, _ in caption_paths:
            inputs += ["-i", path]

        caption_y = int(VIDEO_HEIGHT * 0.66)
        filter_parts = []
        last_label = "0:v"
        for i, (_, start, end) in enumerate(caption_paths):
            out_label = f"v{i + 1}"
            filter_parts.append(
                f"[{last_label}][{i + 1}:v]overlay=x=0:y={caption_y}:"
                f"enable='between(t,{start:.2f},{end:.2f})'[{out_label}]"
            )
            last_label = out_label

        command = [
            "ffmpeg", "-y",
            *inputs,
            "-filter_complex", ";".join(filter_parts),
            "-map", f"[{last_label}]",
            "-map", "0:a",
            "-c:v", "libx264", "-c:a", "copy",
            "-pix_fmt", "yuv420p",
            TEMP_VIDEO,
        ]
        subprocess.run(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)
        return True
    except Exception as e:
        print(f"⚠️ تعذر حرق الترجمة بالفيديو (سيُنشر بدونها): {str(e)[:200]}")
        return False
    finally:
        shutil.rmtree(CAPTION_DIR, ignore_errors=True)


def create_video_reel():
    print("🎬 جاري المونتاج وإضافة الحركة البصرية...")
    try:
        command = [
            "ffmpeg", "-y", "-loop", "1", "-framerate", "30", "-i", TEMP_IMAGE, "-i", TEMP_AUDIO,
            "-vf", f"zoompan=z='min(zoom+0.0005,1.15)':x='iw/2-(iw/zoom)/2':y='ih/2-(ih/zoom)/2':d=1500:s={VIDEO_WIDTH}x{VIDEO_HEIGHT}:fps=30",
            "-c:v", "libx264", "-c:a", "aac", "-b:a", "192k",
            "-pix_fmt", "yuv420p", "-shortest", TEMP_BASE_VIDEO,
        ]
        subprocess.run(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)
    except Exception as e:
        print(f"❌ خطأ في المونتاج: {e}")
        return False

    print("💬 جاري إضافة ترجمة عربية متزامنة مع الصوت...")
    if not _burn_captions(TEMP_BASE_VIDEO):
        os.replace(TEMP_BASE_VIDEO, TEMP_VIDEO)
        return True

    if os.path.exists(TEMP_BASE_VIDEO):
        os.remove(TEMP_BASE_VIDEO)
    return True


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
    for path in (TEMP_IMAGE, TEMP_AUDIO, TEMP_BASE_VIDEO, TEMP_VIDEO):
        if os.path.exists(path):
            os.remove(path)
    shutil.rmtree(CAPTION_DIR, ignore_errors=True)


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
