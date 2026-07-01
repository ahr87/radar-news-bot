import os
import requests
import time
import subprocess
import schedule
from flask import Flask
from threading import Thread
from openai import OpenAI

# المفاتيح من بيئة ريبليت
ACCESS_TOKEN = os.environ.get('IG_ACCESS_TOKEN')
INSTAGRAM_ACCOUNT_ID = os.environ.get('IG_ACCOUNT_ID')
OPENAI_API_KEY = os.environ.get('OPENAI_API_KEY')
MEMORY_FILE = "published_topics.txt"

client = OpenAI(api_key=OPENAI_API_KEY)
app = Flask(__name__)

@app.route('/')
def home():
    """هذه الصفحة تبقي السيرفر مستيقظاً"""
    return "Radar News Bot is Alive and Running!"

def run_server():
    app.run(host='0.0.0.0', port=5000)

def get_previous_topics():
    if os.path.exists(MEMORY_FILE):
        with open(MEMORY_FILE, "r", encoding="utf-8") as file:
            return file.read().strip()
    return "لا توجد مواضيع سابقة."

def save_topic_to_memory(topic_summary):
    titles = []
    if os.path.exists(MEMORY_FILE):
        with open(MEMORY_FILE, "r", encoding="utf-8") as file:
            titles = file.read().splitlines()
    titles.insert(0, topic_summary)
    with open(MEMORY_FILE, "w", encoding="utf-8") as file:
        file.write("\n".join(titles[:20]))

def generate_exclusive_content():
    print("🧠 جاري ابتكار سيناريو مشوق (بالفصحى)...")
    previous_topics = get_previous_topics()

    text_prompt = f"""
    أنت صانع محتوى ويوتيوبر محترف تدير حساب انستقرام عراقي اسمه 'رادار نيوز'.
    استخرج "حقيقة علمية نادرة جداً" لا يعرفها الناس. تجنب: [{previous_topics}].

    استخدم هذا التنسيق بالضبط في ردك:
    الملخص: (كلمتين عن الموضوع)
    الصوت: (اكتب سيناريو التعليق الصوتي بحدود 40 إلى 50 كلمة. ابدأ بـ Hook قوي جداً. يجب أن يكون النص باللغة العربية الفصحى المبسطة والسليمة 100% لتجنب أخطاء النطق).
    الكابشن: (جملة تدعو للتعليق + 5 هاشتاجات)
    """

    try:
        response = client.chat.completions.create(
            model="gpt-4o",
            messages=[{"role": "user", "content": text_prompt}],
            temperature=0.8
        )
        full_text = response.choices[0].message.content.strip()

        topic_summary = "موضوع علمي"
        voice_script = "هل تعلم أن هناك حقيقة علمية ستغير نظرتك للعالم؟"
        caption = full_text

        for line in full_text.split('\n'):
            if line.startswith("الملخص:"):
                topic_summary = line.replace("الملخص:", "").replace("(", "").replace(")", "").strip()
            elif line.startswith("الصوت:"):
                voice_script = line.replace("الصوت:", "").replace("(", "").replace(")", "").strip()

        if "الكابشن:" in full_text:
            caption = full_text.split("الكابشن:")[1].strip()

        print(f"✅ تم كتابة السيناريو: {topic_summary}")

        print("🎨 جاري رسم اللوحة السريالية...")
        image_response = client.images.generate(
            model="dall-e-3",
            prompt=f"Create a highly conceptual, surreal, vertical (9:16) illustration about: '{topic_summary}'. No text. Masterpiece, highly detailed, cinematic lighting.",
            size="1024x1792",
            quality="standard",
            n=1,
        )
        image_url = image_response.data[0].url

        img_data = requests.get(image_url).content
        with open("temp_image.jpg", "wb") as handler:
            handler.write(img_data)

        return topic_summary, voice_script, caption
    except Exception as e:
        print(f"❌ خطأ في توليد المحتوى: {e}")
        return None, None, None

def generate_voice_over(text):
    print(f"🎙️ جاري تسجيل الصوت بجودة HD بالفصحى...")
    try:
        response = client.audio.speech.create(
            model="tts-1-hd",
            voice="onyx", 
            input=text
        )
        with open("temp_audio.mp3", "wb") as f:
            f.write(response.content)
        return True
    except Exception as e:
        print(f"❌ خطأ في توليد الصوت: {e}")
        return False

def create_video_reel():
    print("🎬 جاري المونتاج وإضافة الحركة البصرية...")
    try:
        command = [
            "ffmpeg", "-y", "-loop", "1", "-framerate", "30", "-i", "temp_image.jpg", "-i", "temp_audio.mp3",
            "-vf", "zoompan=z='min(zoom+0.0005,1.15)':x='iw/2-(iw/zoom)/2':y='ih/2-(ih/zoom)/2':d=1500:s=1024x1792:fps=30",
            "-c:v", "libx264", "-c:a", "aac", "-b:a", "192k",
            "-pix_fmt", "yuv420p", "-shortest", "output_reel.mp4"
        ]
        subprocess.run(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return True
    except Exception as e:
        print(f"❌ خطأ في المونتاج: {e}")
        return False

def upload_to_temp_server():
    print("🌐 جاري الرفع للسيرفر المؤقت (tmpfiles.org)...")
    try:
        with open("output_reel.mp4", "rb") as f:
            res = requests.post("https://tmpfiles.org/api/v1/upload", files={"file": f}).json()
        direct_url = res['data']['url'].replace("tmpfiles.org/", "tmpfiles.org/dl/")
        return direct_url
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

def job():
    """دورة الإنتاج الآلية"""
    print("⏳ حان وقت النشر المجدول! بدء دورة الإنتاج...")
    topic_summary, voice_script, caption = generate_exclusive_content()

    if topic_summary and voice_script and caption:
        if generate_voice_over(voice_script):
            if create_video_reel():
                public_video_url = upload_to_temp_server()
                if public_video_url:
                    if post_reel_to_instagram(public_video_url, caption):
                        save_topic_to_memory(topic_summary)
                        if os.path.exists("temp_image.jpg"): os.remove("temp_image.jpg")
                        if os.path.exists("temp_audio.mp3"): os.remove("temp_audio.mp3")
                        if os.path.exists("output_reel.mp4"): os.remove("output_reel.mp4")

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