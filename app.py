import math
import time
import queue
import threading
import json
import streamlit as st
import streamlit.components.v1 as components

try:
    import av
except Exception:
    av = None

CV2_ERROR = None
try:
    import cv2
except Exception as e:
    cv2 = None
    CV2_ERROR = repr(e)

try:
    import mediapipe as mp
except Exception:
    mp = None

try:
    from streamlit_webrtc import webrtc_streamer, VideoProcessorBase, WebRtcMode
except Exception:
    webrtc_streamer = None
    VideoProcessorBase = object
    WebRtcMode = None

# -------------------- App setup --------------------
st.set_page_config(
    page_title="AI Study Buddy",
    page_icon="📚",
    layout="centered",
    initial_sidebar_state="collapsed",
)

st.markdown("""
<style>
#MainMenu, footer, header, [data-testid="stSidebar"], [data-testid="stToolbar"] {visibility: hidden; height: 0;}
.main .block-container {max-width: 760px; padding-top: 1.1rem; padding-bottom: 2rem;}
.hero {text-align:center; padding: 1.2rem .7rem .7rem;}
.hero h1 {margin-bottom:.2rem; font-size:2.15rem;}
.hero p {opacity:.72; margin-top:0;}
div[data-testid="stSelectbox"] {max-width: 300px; margin: 0 auto;}
</style>
""", unsafe_allow_html=True)

st.markdown(
    '<div class="hero"><h1>📚 AI Study Buddy</h1>'
    '<p>Your camera-based study companion — with funny voice reminders.</p></div>',
    unsafe_allow_html=True,
)

language = st.selectbox("Alert language", ["Roman English", "Urdu", "English"], index=0)

MESSAGES = {
    "Roman English": {
        "book": [
            "Oye student! Book kholo, Instagram ki reel nahi hai yeh!",
            "Kitab samne aa gayi, ab parhna bhi shuru kar do boss!",
            "Book ko decoration nahi, parhai ke liye rakha hai!",
        ],
        "sleep": [
            "Oye professor sahab, class mein so rahe ho ya research kar rahe ho?",
            "Aankhein band? Sapno mein degree nahi milni, thora jag jao!",
            "Neend ko bolo break ke baad aana, abhi parhai chal rahi hai!",
        ],
        "away": [
            "Student kidhar gaye? Camera ko lecture de kar khud gayab!",
        ],
    },
    "Urdu": {
        "book": [
            "ارے طالب علم! کتاب کھولو، ریلز بعد میں دیکھ لینا!",
            "کتاب سامنے آگئی، اب پڑھ بھی لو جناب!",
            "کتاب سجاوٹ کے لیے نہیں، پڑھنے کے لیے ہے!",
        ],
        "sleep": [
            "جناب کلاس میں سو رہے ہیں یا خوابوں میں ڈگری لے رہے ہیں؟",
            "آنکھیں بند ہیں؟ ذرا جاگ جائیں، امتحان خواب میں نہیں ہوگا!",
            "نیند کو کہیں وقفے میں آنا، ابھی پڑھائی جاری ہے!",
        ],
        "away": [
            "طالب علم کہاں چلے گئے؟ کیمرے کو اکیلا چھوڑ دیا!",
        ],
    },
    "English": {
        "book": [
            "Hey scholar, open the book. Reels can wait!",
            "The book is here. Now let the studying begin!",
            "Books are for reading, not just desk decoration!",
        ],
        "sleep": [
            "Professor, are you studying or auditioning for a nap?",
            "Eyes closed? Your degree will not arrive in a dream!",
            "Tell sleep to come back during the break!",
        ],
        "away": [
            "Student missing! Did you leave the camera to study alone?",
        ],
    },
}


def speak_in_browser(message: str, lang: str, event_id: int):
    """Speak using the PARENT page's speechSynthesis.

    Fix: the old version spoke from inside the small iframe. That iframe was
    destroyed on the next auto-refresh (1.2 s), which cut the voice off, and
    browsers often block speech inside iframes. The same HTML is now rendered
    on every rerun (so the iframe is not re-created) and a guard stops the
    same event from being spoken twice.
    """
    lang_code = {"English": "en-US", "Urdu": "ur-PK", "Roman English": "en-IN"}[lang]
    safe_message = json.dumps(message, ensure_ascii=False)
    components.html(
        f"""
        <script>
        (() => {{
          const message = {safe_message};
          const eventId = {event_id};
          const lang = "{lang_code}";
          let w = window;
          try {{ if (window.parent && window.parent.speechSynthesis) w = window.parent; }} catch (e) {{}}
          if (!("speechSynthesis" in w)) return;
          if (w.__studyBuddySpoken === eventId) return;
          w.__studyBuddySpoken = eventId;
          const speak = () => {{
            const synth = w.speechSynthesis;
            synth.cancel();
            const u = new w.SpeechSynthesisUtterance(message);
            u.lang = lang;
            u.rate = 1.0;
            u.pitch = 1.1;
            const voices = synth.getVoices();
            const base = lang.split("-")[0];
            const v = voices.find(x => x.lang === lang) ||
                      voices.find(x => x.lang.toLowerCase().startsWith(base));
            if (v) u.voice = v;
            synth.speak(u);
          }};
          if (w.speechSynthesis.getVoices().length === 0) {{
            w.speechSynthesis.onvoiceschanged = speak;
            setTimeout(speak, 400);
          }} else {{
            speak();
          }}
        }})();
        </script>
        """,
        height=0,
    )


# -------------------- Session state --------------------
if "study_alert_queue" not in st.session_state:
    st.session_state.study_alert_queue = queue.Queue()
if "study_alert_event_id" not in st.session_state:
    st.session_state.study_alert_event_id = 0
if "current_alert" not in st.session_state:
    st.session_state.current_alert = None

alert_queue = st.session_state.study_alert_queue


# -------------------- Video processor (module level, not redefined per rerun) --------------------
class StudyBuddyProcessor(VideoProcessorBase):
    COOLDOWN = {"book": 18, "sleep": 22, "away": 25}
    EAR_THRESHOLD = 0.19
    SLEEP_SECONDS = 2.5
    AWAY_SECONDS = 8.0
    BOOK_CONFIRM_CHECKS = 10  # book check runs every 3rd frame -> ~2 seconds

    def __init__(self):
        # The main Streamlit thread replaces this with the session queue.
        self.alert_queue = queue.Queue()
        self.lock = threading.Lock()
        self.face_mesh = None
        self.frame_number = 0
        self.closed_since = None
        self.no_face_since = None
        self.book_checks = 0
        self.last_book_box = None
        self.last_alert_time = {}
        if mp is not None:
            try:
                self.face_mesh = mp.solutions.face_mesh.FaceMesh(
                    static_image_mode=False,
                    max_num_faces=1,
                    refine_landmarks=True,
                    min_detection_confidence=0.55,
                    min_tracking_confidence=0.55,
                )
            except Exception:
                self.face_mesh = None

    @staticmethod
    def eye_aspect_ratio(landmarks, indexes, width, height):
        pts = [(landmarks[i].x * width, landmarks[i].y * height) for i in indexes]

        def dist(a, b):
            return math.hypot(a[0] - b[0], a[1] - b[1])

        horizontal = dist(pts[0], pts[3])
        if horizontal <= 0:
            return 1.0
        return (dist(pts[1], pts[5]) + dist(pts[2], pts[4])) / (2.0 * horizontal)

    def push_alert(self, kind):
        now = time.time()
        with self.lock:
            if now - self.last_alert_time.get(kind, 0) < self.COOLDOWN.get(kind, 20):
                return
            self.last_alert_time[kind] = now
        try:
            self.alert_queue.put_nowait((kind, now))
        except Exception:
            pass

    def detect_book(self, small):
        """Rough heuristic: large rectangular contour. Can mistake screens/notebooks for books."""
        sh, sw = small.shape[:2]
        gray = cv2.cvtColor(small, cv2.COLOR_BGR2GRAY)
        blurred = cv2.GaussianBlur(gray, (5, 5), 0)
        edges = cv2.Canny(blurred, 55, 145)
        contours, _ = cv2.findContours(edges, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        for contour in sorted(contours, key=cv2.contourArea, reverse=True)[:12]:
            if cv2.contourArea(contour) < sw * sh * 0.075:
                continue
            x, y, w, h = cv2.boundingRect(contour)
            ratio = w / max(h, 1)
            if 0.52 <= ratio <= 1.65 and h > sh * 0.20:
                return (x / sw, y / sh, w / sw, h / sh)  # normalized box
        return None

    def recv(self, frame):
        image = frame.to_ndarray(format="bgr24")
        height, width = image.shape[:2]
        self.frame_number += 1
        now = time.time()

        # Work on a smaller copy so the video stays smooth.
        scale = 480.0 / width if width > 480 else 1.0
        small = cv2.resize(image, (int(width * scale), int(height * scale))) if scale < 1.0 else image

        # ---- Book heuristic (every 3rd frame, must persist for ~2 s) ----
        if self.frame_number % 3 == 0:
            try:
                box = self.detect_book(small)
                self.last_book_box = box
                if box:
                    self.book_checks += 1
                else:
                    self.book_checks = max(0, self.book_checks - 1)
                if self.book_checks >= self.BOOK_CONFIRM_CHECKS:
                    self.push_alert("book")
                    self.book_checks = 0
            except Exception:
                pass

        if self.last_book_box:
            nx, ny, nw, nh = self.last_book_box
            x, y, w, h = int(nx * width), int(ny * height), int(nw * width), int(nh * height)
            cv2.rectangle(image, (x, y), (x + w, y + h), (50, 205, 130), 2)
            cv2.putText(image, "Possible book/object", (x, max(20, y - 8)),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.55, (50, 205, 130), 2)

        # ---- Drowsiness / away (time based, not frame-count based) ----
        if self.face_mesh is not None:
            try:
                rgb = cv2.cvtColor(small, cv2.COLOR_BGR2RGB)
                result = self.face_mesh.process(rgb)
                if result.multi_face_landmarks:
                    self.no_face_since = None
                    points = result.multi_face_landmarks[0].landmark
                    left = self.eye_aspect_ratio(points, [33, 160, 158, 133, 153, 144], width, height)
                    right = self.eye_aspect_ratio(points, [362, 385, 387, 263, 373, 380], width, height)
                    ear = (left + right) / 2
                    for p in points[::20]:
                        cv2.circle(image, (int(p.x * width), int(p.y * height)), 1, (255, 190, 40), -1)
                    if ear < self.EAR_THRESHOLD:
                        if self.closed_since is None:
                            self.closed_since = now
                        elif now - self.closed_since >= self.SLEEP_SECONDS:
                            self.push_alert("sleep")
                            self.closed_since = now
                        if self.closed_since is not None and now - self.closed_since > 1.0:
                            cv2.putText(image, "Possible drowsiness", (10, height - 20),
                                        cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 80, 255), 2)
                    else:
                        self.closed_since = None
                else:
                    self.closed_since = None
                    if self.no_face_since is None:
                        self.no_face_since = now
                    elif now - self.no_face_since >= self.AWAY_SECONDS:
                        self.push_alert("away")
                        self.no_face_since = now
            except Exception:
                pass

        cv2.putText(image, "AI Study Buddy", (10, 28), cv2.FONT_HERSHEY_SIMPLEX,
                    0.7, (255, 255, 255), 2, cv2.LINE_AA)
        return av.VideoFrame.from_ndarray(image, format="bgr24")


# -------------------- UI --------------------
if cv2 is None:
    st.error("OpenCV did not load. Check requirements.txt and packages.txt (libgl1).")
    st.code(CV2_ERROR or "unknown import error")
elif av is None:
    st.error("PyAV (av) did not load. Check requirements.txt.")
elif webrtc_streamer is None:
    st.error("The camera component did not load. Check that streamlit-webrtc installed correctly.")
else:
    st.markdown("### Live camera")
    st.caption("Press START and allow camera access in your browser. Keep your face and study desk visible.")

    ctx = webrtc_streamer(
        key="ai-study-buddy-camera-v3",
        mode=WebRtcMode.SENDRECV,
        video_processor_factory=StudyBuddyProcessor,
        # STUN servers: without these the camera often stays on "connecting" when deployed.
        rtc_configuration={
            "iceServers": [
                {"urls": ["stun:stun.l.google.com:19302", "stun:stun1.l.google.com:19302"]}
            ]
        },
        media_stream_constraints={
            "video": {"width": {"ideal": 640}, "height": {"ideal": 480}},
            "audio": False,
        },
        async_processing=True,
    )

    # Connect the processing thread to this session's queue.
    if ctx.video_processor is not None:
        ctx.video_processor.alert_queue = alert_queue

    # Poll while the camera is playing so thread events reach the page.
    try:
        from streamlit_autorefresh import st_autorefresh
        if ctx.state.playing:
            st_autorefresh(interval=1200, key="study_buddy_poll")
    except Exception:
        pass

    # Drain the queue; keep only the newest alert.
    latest_alert = None
    try:
        while not alert_queue.empty():
            latest_alert = alert_queue.get_nowait()
    except Exception:
        pass

    if latest_alert:
        kind, timestamp = latest_alert
        st.session_state.study_alert_event_id += 1
        message_list = MESSAGES[language].get(kind, MESSAGES[language]["book"])
        message = message_list[(int(timestamp) // 17) % len(message_list)]
        st.session_state.current_alert = {
            "message": message,
            "lang": language,
            "id": st.session_state.study_alert_event_id,
        }

    # Fix: the alert stays on screen (and the speaker iframe stays mounted)
    # instead of vanishing on the next 1.2 s refresh.
    alert = st.session_state.current_alert
    if alert:
        st.info("🔊 " + alert["message"])
        speak_in_browser(alert["message"], alert["lang"], alert["id"])

st.caption("Privacy: camera access requires browser permission. This prototype does not save video frames to disk.")
st.caption("Book detection is experimental. Eye-closure alerts can be inaccurate; use this as a playful reminder, not as a safety or medical tool.")
