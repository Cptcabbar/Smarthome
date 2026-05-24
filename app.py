#!/usr/bin/env python3
"""
Smart Home Control System
Raspberry Pi 5 - Flask + faster-whisper + SQLite + Ollama keyword üretimi
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import re
import signal
import subprocess
import tempfile
import threading
import time
from difflib import SequenceMatcher
from pathlib import Path

import paho.mqtt.client as paho_mqtt
import requests
from flask import Flask, jsonify, render_template, request

import db

try:
    from flask_sock import Sock as _FlaskSock
    SOCK_AVAILABLE = True
except ImportError:
    SOCK_AVAILABLE = False
    log_placeholder = logging.getLogger(__name__)
    log_placeholder.warning("flask-sock kurulu değil — Live API WS devre dışı")

try:
    import google.genai as _genai_mod
    GENAI_AVAILABLE = True
except ImportError:
    GENAI_AVAILABLE = False

try:
    from apscheduler.schedulers.background import BackgroundScheduler as _APSched
    from apscheduler.triggers.cron import CronTrigger as _CronTrigger
    APS_AVAILABLE = True
except ImportError:
    APS_AVAILABLE = False

# ── Logging ──────────────────────────────────────────────────────────────────
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger(__name__)

# ── App ───────────────────────────────────────────────────────────────────────
_START_TIME = str(int(time.time()))
app = Flask(__name__)
if SOCK_AVAILABLE:
    sock = _FlaskSock(app)

# ── Env ───────────────────────────────────────────────────────────────────────
OLLAMA_HOST = os.environ.get("OLLAMA_HOST", "http://127.0.0.1:11434").rstrip("/")
OLLAMA_MODEL = os.environ.get("OLLAMA_MODEL", "qwen2.5:1.5b")
WHISPER_MODEL = os.environ.get("WHISPER_MODEL", "base")
WHISPER_DEVICE = os.environ.get("WHISPER_DEVICE", "cpu")
WHISPER_COMPUTE_TYPE = os.environ.get("WHISPER_COMPUTE_TYPE", "int8")
WHISPER_BEAM_SIZE = int(os.environ.get("WHISPER_BEAM_SIZE", "1"))
PORT = int(os.environ.get("PORT", "3000"))
MQTT_HOST = os.environ.get("MQTT_HOST", "127.0.0.1")
MQTT_PORT = int(os.environ.get("MQTT_PORT", "1883"))
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "models/gemini-3.1-flash-live-preview")
GEMINI_TEXT_MODEL = os.environ.get("GEMINI_TEXT_MODEL", "gemini-flash-latest")


def _load_gemini_key_from_db() -> None:
    """DB'de kayıtlı anahtar varsa env'i geçersiz kılar (kullanıcı ayarladıysa)."""
    global GEMINI_API_KEY
    try:
        stored = db.get_setting("gemini_api_key")
        if stored:
            GEMINI_API_KEY = stored
    except Exception:
        pass

# ── faster-whisper (lazy load) ────────────────────────────────────────────────
_whisper_model = None


def get_whisper_model():
    global _whisper_model
    if _whisper_model is None:
        try:
            from faster_whisper import WhisperModel

            log.info(
                "faster-whisper yükleniyor: model=%s device=%s compute_type=%s beam_size=%d",
                WHISPER_MODEL,
                WHISPER_DEVICE,
                WHISPER_COMPUTE_TYPE,
                WHISPER_BEAM_SIZE,
            )
            _whisper_model = WhisperModel(
                WHISPER_MODEL,
                device=WHISPER_DEVICE,
                compute_type=WHISPER_COMPUTE_TYPE,
                num_workers=2,
                cpu_threads=4,
            )
            log.info("faster-whisper hazır.")
        except ImportError:
            log.warning("faster-whisper kurulu değil.")
    return _whisper_model


def transcribe_audio(path: str) -> str:
    model = get_whisper_model()
    if not model:
        raise RuntimeError(
            "Ses tanıma kullanılamıyor: faster-whisper kurulu değil. "
            "pip install faster-whisper ve ffmpeg kurun."
        )
    segments, _info = model.transcribe(
        path,
        language="tr",
        beam_size=WHISPER_BEAM_SIZE,
        temperature=0.0,
        vad_filter=True,
        vad_parameters={"min_silence_duration_ms": 300},
        condition_on_previous_text=False,
        word_timestamps=False,
        no_speech_threshold=0.6,
        compression_ratio_threshold=2.4,
    )
    return "".join(seg.text for seg in segments).strip()


# ── GPIO (Pi 5 → lgpio, eski Pi → RPi.GPIO, diğer → simülasyon) ──────────────
USE_GPIO = False
_lgpio_handle = None

try:
    import lgpio as _lgpio_mod
    _lgpio_handle = _lgpio_mod.gpiochip_open(0)
    USE_GPIO = True
    log.info("GPIO aktif (lgpio — Raspberry Pi 5).")
except (ImportError, Exception):
    try:
        import RPi.GPIO as _rpigpio_mod
        _rpigpio_mod.setmode(_rpigpio_mod.BCM)
        USE_GPIO = True
        log.info("GPIO aktif (RPi.GPIO).")
    except (ImportError, RuntimeError):
        log.info("GPIO bulunamadı — simülasyon modunda çalışılıyor.")


def _gpio_setup_pin(pin: int) -> None:
    if not USE_GPIO or pin <= 0:
        return
    if _lgpio_handle is not None:
        import lgpio as _lgpio_mod
        _lgpio_mod.gpio_claim_output(_lgpio_handle, pin, 0)
    else:
        import RPi.GPIO as _rpigpio_mod
        _rpigpio_mod.setup(pin, _rpigpio_mod.OUT, initial=_rpigpio_mod.LOW)


def _gpio_output(pin: int, state: bool) -> None:
    if not USE_GPIO or pin <= 0:
        return
    if _lgpio_handle is not None:
        import lgpio as _lgpio_mod
        _lgpio_mod.gpio_write(_lgpio_handle, pin, 1 if state else 0)
    else:
        import RPi.GPIO as _rpigpio_mod
        _rpigpio_mod.output(pin, _rpigpio_mod.HIGH if state else _rpigpio_mod.LOW)


def gpio_init_all_devices() -> None:
    if not USE_GPIO:
        return
    for dev in db.list_devices_dict().values():
        p = int(dev.get("pin") or 0)
        if p > 0:
            try:
                _gpio_setup_pin(p)
            except Exception as e:
                log.warning("GPIO pin %d meşgul, atlandı: %s", p, e)


# ── MQTT istemcisi ────────────────────────────────────────────────────────────
_mqtt_client: paho_mqtt.Client | None = None


def _init_mqtt() -> None:
    global _mqtt_client
    try:
        c = paho_mqtt.Client(
            paho_mqtt.CallbackAPIVersion.VERSION2,
            client_id="smarthome-flask",
        )
        c.connect(MQTT_HOST, MQTT_PORT, keepalive=60)
        c.loop_start()
        _mqtt_client = c
        log.info("MQTT bağlantısı kuruldu: %s:%d", MQTT_HOST, MQTT_PORT)
    except Exception as e:
        log.warning("MQTT bağlantısı kurulamadı (simülasyon devam eder): %s", e)
        _mqtt_client = None


# Keyword üretimi süren cihaz ID'leri
_generating_keywords: set[str] = set()
_generating_lock = threading.Lock()


def _mqtt_publish(topic: str, payload: str) -> None:
    if _mqtt_client is None:
        log.warning("MQTT bağlı değil — mesaj gönderilemedi: %s → %s", topic, payload)
        return
    try:
        _mqtt_client.publish(topic, payload, qos=1)
        log.info("MQTT yayımlandı: %s → %s", topic, payload)
    except Exception as e:
        log.warning("MQTT publish hatası: %s", e)


# ── TTS (gTTS + mpg123) ───────────────────────────────────────────────────────
TTS_DIR = Path(__file__).resolve().parent / "tts_cache"
TTS_DIR.mkdir(exist_ok=True)


def _tts_path(text: str) -> Path:
    import hashlib
    return TTS_DIR / (hashlib.md5(text.encode()).hexdigest() + ".mp3")


def _ensure_tts(text: str) -> Path | None:
    path = _tts_path(text)
    if path.exists():
        return path
    try:
        from gtts import gTTS
        gTTS(text=text, lang="tr").save(str(path))
        log.info("TTS önbelleğe alındı: '%s'", text)
        return path
    except Exception as e:
        log.warning("TTS üretme hatası: %s", e)
        return None


def speak(text: str) -> None:
    def _play():
        path = _ensure_tts(text)
        if not path:
            return
        try:

            subprocess.run(["mpg123", "-q", str(path)], timeout=15)
        except Exception as e:
            log.warning("TTS oynatma hatası: %s", e)
    threading.Thread(target=_play, daemon=True).start()


def prewarm_tts(label: str) -> None:
    """Cihaz için her iki ses dosyasını arka planda hazırlar."""
    def _gen():
        for suffix in ("açıldı", "kapatıldı"):
            _ensure_tts(f"{label} {suffix}")
    threading.Thread(target=_gen, daemon=True).start()


# ── Ollama: keyword üretimi ───────────────────────────────────────────────────
def fallback_keywords_from_name(name: str) -> list[str]:
    name = (name or "").strip().lower()
    if not name:
        return []
    # Basit normalize (Türkçe için kaba)
    simplified = (
        name.replace("ı", "i")
        .replace("İ", "i")
        .replace("ğ", "g")
        .replace("ü", "u")
        .replace("ş", "s")
        .replace("ö", "o")
        .replace("ç", "c")
    )
    tokens = re.split(r"\s+", simplified)
    tokens = [t for t in tokens if len(t) >= 2]
    out = list({name, simplified, *tokens})
    return [x for x in out if x][:24]


def generate_keywords_ollama(device_name: str) -> list[str]:
    """Ollama ile JSON array keyword döndürür; başarısız olursa boş liste."""
    prompt = (
        'Sen bir akıllı ev asistanısın. Aşağıdaki cihaz adı için sesli komutta '
        'kullanılabilecek anahtar kelime ve kısa ifadeler üret. Sadece JSON dizisi döndür, '
        'başka metin yazma. Örnek çıktı: ["mutfak lambası","mutfak ışığı","ışıkları aç"]\n\n'
        f'Cihaz adı: "{device_name.strip()}"'
    )
    url = f"{OLLAMA_HOST}/api/chat"
    body = {
        "model": OLLAMA_MODEL,
        "messages": [{"role": "user", "content": prompt}],
        "stream": False,
    }
    try:
        r = requests.post(url, json=body, timeout=120)
        r.raise_for_status()
        data = r.json()
        content = (data.get("message") or {}).get("content") or ""
        content = content.strip()
        # JSON array çıkarmayı dene
        m = re.search(r"\[[\s\S]*\]", content)
        if m:
            content = m.group(0)
        arr = json.loads(content)
        if not isinstance(arr, list):
            return []
        seen = set()
        out: list[str] = []
        for x in arr:
            s = str(x).lower().strip()
            if s and s not in seen:
                seen.add(s)
                out.append(s)
            if len(out) >= 32:
                break
        return out
    except Exception as e:
        log.warning("Ollama keyword üretimi başarısız: %s", e)
        return []


def merge_keywords(ai_list: list[str], name: str) -> list[str]:
    base = fallback_keywords_from_name(name)
    seen = set()
    out: list[str] = []
    for w in ai_list + base:
        w = w.lower().strip()
        if w and w not in seen:
            seen.add(w)
            out.append(w)
        if len(out) >= 40:
            break
    return out


# ── Komut eşleştirme ─────────────────────────────────────────────────────────
OPEN_WORDS = [
    "aç",
    "çalıştır",
    "başlat",
    "ac",
    "yak",
    "etkinleştir",
    "open",
    "turn on",
    "start",
]
CLOSE_WORDS = [
    "kapat",
    "kapa",
    "durdur",
    "söndür",
    "close",
    "turn off",
    "stop",
]


def _token_similarity(a: str, b: str) -> float:
    return SequenceMatcher(None, a, b).ratio()


def normalize_text(text: str) -> str:
    t = text.lower().strip()
    t = (
        t.replace("ı", "i")
        .replace("İ", "i")
        .replace("ğ", "g")
        .replace("ü", "u")
        .replace("ş", "s")
        .replace("ö", "o")
        .replace("ç", "c")
    )
    return t


def parse_voice_command_ai(text: str, devices: dict[str, dict]) -> tuple[str | None, str | None]:
    """Ollama ile doğal dil anlama — keyword listesine bağımlı değil."""
    device_list = [{"id": did, "name": dev["label"]} for did, dev in devices.items()]
    prompt = (
        "Sen bir akıllı ev asistanısın. Türkçe sesli komutu analiz et ve hangi cihazın "
        "ne yapılacağını belirle. Sadece JSON döndür, başka hiçbir metin yazma.\n\n"
        f"Cihazlar: {json.dumps(device_list, ensure_ascii=False)}\n"
        f"Komut: \"{text}\"\n\n"
        'Çıktı: {"device_id": "<id>", "action": "on"} veya {"device_id": "<id>", "action": "off"}\n'
        'Eşleşme yoksa: {"device_id": null, "action": null}'
    )
    url = f"{OLLAMA_HOST}/api/chat"
    body = {
        "model": OLLAMA_MODEL,
        "messages": [{"role": "user", "content": prompt}],
        "stream": False,
    }
    try:
        r = requests.post(url, json=body, timeout=15)
        r.raise_for_status()
        content = (r.json().get("message") or {}).get("content", "").strip()
        m = re.search(r"\{[\s\S]*?\}", content)
        if m:
            result = json.loads(m.group(0))
            device_id = result.get("device_id")
            action = result.get("action")
            if device_id in devices and action in ("on", "off"):
                log.info("AI komut: cihaz=%s aksiyon=%s", devices[device_id]["label"], action)
                return device_id, action
        log.warning("AI komut yanıtı geçersiz: %s", content[:200])
    except Exception as e:
        log.warning("AI komut ayrıştırma başarısız: %s", e)
    return None, None


def parse_voice_command_keyword(text: str, devices: dict[str, dict]) -> tuple[str | None, str | None]:
    text_norm = normalize_text(text)
    text_tokens = re.split(r"\s+", text_norm)

    action = None
    for w in OPEN_WORDS:
        nw = normalize_text(w)
        if nw in text_norm or any(t.startswith(nw) for t in text_tokens if len(nw) >= 3):
            action = "on"
            break
    if action is None:
        for w in CLOSE_WORDS:
            nw = normalize_text(w)
            if nw in text_norm or any(t.startswith(nw) for t in text_tokens if len(nw) >= 3):
                action = "off"
                break
    if action is None:
        return None, None

    best_id = None
    best_score = 0
    for device_id, dev in devices.items():
        score = 0
        # Cihaz adı tokenları — prefix + fuzzy matching (Türkçe transkripsiyon hatalarına karşı)
        name_tokens = [t for t in re.split(r"\s+", normalize_text(dev["label"])) if len(t) >= 3]
        for nt in name_tokens:
            for tt in text_tokens:
                if tt.startswith(nt):
                    score += len(nt) + 5
                elif len(tt) >= 3:
                    sim = _token_similarity(tt, nt)
                    if sim >= 0.6:
                        score += int(sim * len(nt))
        # Ek keyword varsa bonus
        for kw in dev.get("keywords") or []:
            kn = normalize_text(kw)
            if len(kn) >= 3 and kn in text_norm:
                score = max(score, len(kn) + 10)
        if score > best_score:
            best_score = score
            best_id = device_id

    return (best_id, action) if best_id and best_score > 0 else (None, None)


def parse_voice_command(text: str, devices: dict[str, dict]) -> tuple[str | None, str | None]:
    log.info("Komut ayrıştırılıyor: '%s'", text[:200])
    return parse_voice_command_keyword(text, devices)


# ── Cihaz I/O ────────────────────────────────────────────────────────────────
def set_device(device_id: str, state: bool, source: str = "manual") -> bool:
    dev = db.get_device(device_id)
    if not dev:
        return False
    db.set_device_state(device_id, state)
    topic = (dev.get("mqtt_topic") or "").strip()
    action = "AÇILDI" if state else "KAPATILDI"
    if topic:
        _mqtt_publish(topic, "AC" if state else "KAPAT")
        log.info("%-12s %s (mqtt:%s)", dev["label"], action, topic)
    else:
        _gpio_output(int(dev["pin"]), state)
        log.info("%-12s %s (pin=%s)", dev["label"], action, dev["pin"] if USE_GPIO else "simüle")
    try:
        db.log_action(device_id, "on" if state else "off", source)
    except Exception as e:
        log.warning("Action log yazılamadı: %s", e)
    return True


def devices_for_template() -> dict[str, dict]:
    return db.list_devices_dict()


# ── Routes ────────────────────────────────────────────────────────────────────
@app.route("/splash")
def splash():
    return render_template("splash.html")


@app.route("/")
def index():
    return render_template("index.html", devices=devices_for_template(), use_gpio=USE_GPIO)


def _device_payload(dev_id: str, v: dict) -> dict:
    with _generating_lock:
        ready = dev_id not in _generating_keywords
    return {
        "label": v["label"],
        "state": v["state"],
        "icon": v["icon"],
        "color": v["color"],
        "pin": v["pin"],
        "mqtt_topic": v["mqtt_topic"],
        "keywords": v["keywords"],
        "keywords_ready": ready,
    }


@app.route("/api/devices", methods=["GET"])
def api_devices_list():
    d = db.list_devices_dict()
    return jsonify({k: _device_payload(k, v) for k, v in d.items()})


@app.route("/api/devices", methods=["POST"])
def api_devices_create():
    data = request.get_json(silent=True) or {}
    name = (data.get("name") or data.get("label") or "").strip()
    icon = (data.get("icon") or "💡").strip() or "💡"
    color = (data.get("color") or "#38bdf8").strip() or "#38bdf8"
    pin = data.get("pin")
    pin_i = int(pin) if pin is not None and str(pin).strip() != "" else None
    if pin_i is not None and pin_i <= 0:
        pin_i = None
    use_ai = data.get("use_ai_keywords", True)
    override_kw = data.get("keywords")

    mqtt_topic = (data.get("mqtt_topic") or "").strip()

    if not name:
        return jsonify({"error": "Cihaz adı gerekli"}), 400

    if isinstance(override_kw, list):
        kws = [str(x).lower().strip() for x in override_kw if str(x).strip()]
    else:
        kws = fallback_keywords_from_name(name)

    try:
        dev_id = db.create_device(name, icon, color, pin_i, kws, mqtt_topic=mqtt_topic)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400

    if not mqtt_topic:
        _gpio_setup_pin(db.get_device(dev_id)["pin"])

    if use_ai and not isinstance(override_kw, list):
        def _bg_keywords(did: str, dname: str) -> None:
            with _generating_lock:
                _generating_keywords.add(did)
            try:
                ai = generate_keywords_ollama(dname)
                if ai:
                    db.update_device(did, keywords=merge_keywords(ai, dname))
                    log.info("Arka plan keyword güncellendi: %s", dname)
            finally:
                with _generating_lock:
                    _generating_keywords.discard(did)
        threading.Thread(target=_bg_keywords, args=(dev_id, name), daemon=True).start()

    prewarm_tts(name)
    dev = db.get_device(dev_id)
    return jsonify({"id": dev_id, **_device_payload(dev_id, dev)})


@app.route("/api/devices/<device_id>", methods=["PATCH"])
def api_devices_patch(device_id: str):
    data = request.get_json(silent=True) or {}
    name = data.get("name")
    icon = data.get("icon")
    color = data.get("color")
    pin = data.get("pin")
    keywords = data.get("keywords")
    mqtt_topic = data.get("mqtt_topic")
    pin_i = int(pin) if pin is not None else None
    kw_list = None
    if isinstance(keywords, list):
        kw_list = [str(x).lower().strip() for x in keywords if str(x).strip()]

    ok = db.update_device(
        device_id,
        name=name if name is not None else None,
        icon=icon,
        color=color,
        pin=pin_i,
        keywords=kw_list,
        mqtt_topic=mqtt_topic,
    )
    if not ok:
        return jsonify({"error": "Cihaz bulunamadı"}), 404
    dev = db.get_device(device_id)
    return jsonify({"id": device_id, **_device_payload(device_id, dev)})


@app.route("/api/devices/<device_id>", methods=["DELETE"])
def api_devices_delete(device_id: str):
    dev = db.get_device(device_id)
    if not dev:
        return jsonify({"error": "Cihaz bulunamadı"}), 404
    db.delete_device(device_id)
    return jsonify({"ok": True})


@app.route("/api/devices/<device_id>/regenerate-keywords", methods=["POST"])
def api_regenerate_keywords(device_id: str):
    dev = db.get_device(device_id)
    if not dev:
        return jsonify({"error": "Cihaz bulunamadı"}), 404
    ai = generate_keywords_ollama(dev["label"])
    kws = merge_keywords(ai, dev["label"]) if ai else fallback_keywords_from_name(dev["label"])
    db.update_device(device_id, keywords=kws)
    dev = db.get_device(device_id)
    return jsonify({"keywords": dev["keywords"]})


@app.route("/api/status")
def api_status():
    d = db.list_devices_dict()
    return jsonify(
        {
            k: {"label": v["label"], "state": v["state"], "icon": v["icon"]}
            for k, v in d.items()
        }
    )


@app.route("/api/toggle", methods=["POST"])
def api_toggle():
    data = request.get_json(silent=True) or {}
    device_id = data.get("device")
    if not device_id or not db.get_device(device_id):
        return jsonify({"error": "Geçersiz cihaz"}), 400
    dev = db.get_device(device_id)
    new_state = not dev["state"]
    set_device(device_id, new_state)
    dev = db.get_device(device_id)
    return jsonify(
        {
            "device": device_id,
            "state": new_state,
            "label": dev["label"],
        }
    )


@app.route("/api/set", methods=["POST"])
def api_set():
    data = request.get_json(silent=True) or {}
    device_id = data.get("device")
    state = data.get("state")
    if not device_id or not isinstance(state, bool) or not db.get_device(device_id):
        return jsonify({"error": "Geçersiz parametre"}), 400
    set_device(device_id, state)
    return jsonify({"device": device_id, "state": state})


@app.route("/api/voice", methods=["POST"])
def api_voice():
    if "audio" not in request.files:
        return jsonify({"error": "Ses dosyası eksik", "success": False}), 400

    audio_file = request.files["audio"]
    suffix = Path(audio_file.filename).suffix or ".webm"

    with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
        audio_file.save(tmp.name)
        tmp_path = tmp.name

    devices = db.list_devices_dict()
    try:
        try:
            text = transcribe_audio(tmp_path)
        except FileNotFoundError as e:
            log.exception("Transkripsiyon dosya hatası")
            return (
                jsonify(
                    {
                        "success": False,
                        "error": "Ses dosyası okunamadı.",
                        "detail": str(e),
                        "transcript": "",
                    }
                ),
                500,
            )
        except RuntimeError as e:
            log.warning("Transkripsiyon: %s", e)
            return (
                jsonify(
                    {
                        "success": False,
                        "error": str(e),
                        "transcript": "",
                    }
                ),
                503,
            )
        except Exception as e:
            log.exception("Transkripsiyon hatası")
            err_msg = str(e).lower()
            hint = ""
            if "ffmpeg" in err_msg or "avconv" in err_msg:
                hint = "ffmpeg kurulu olmalı (ör. brew install ffmpeg veya apt install ffmpeg)."
            return (
                jsonify(
                    {
                        "success": False,
                        "error": "Ses tanıma başarısız.",
                        "detail": str(e),
                        "hint": hint,
                        "transcript": "",
                    }
                ),
                500,
            )

        if not devices:
            return jsonify(
                {
                    "success": False,
                    "transcript": text,
                    "message": "Önce cihaz ekleyin.",
                }
            )

        device_id, action = parse_voice_command(text, devices)

        if device_id and action:
            new_state = action == "on"
            set_device(device_id, new_state, source="voice")
            dev = db.get_device(device_id)
            action_word = "açıldı" if new_state else "kapatıldı"
            speak(f"{dev['label']} {action_word}")
            return jsonify(
                {
                    "success": True,
                    "transcript": text,
                    "device": device_id,
                    "label": dev["label"],
                    "state": new_state,
                }
            )
        return jsonify(
            {
                "success": False,
                "transcript": text,
                "message": "Komut anlaşılamadı.",
            }
        )
    finally:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass


@app.route("/api/ping")
def api_ping():
    return jsonify({"id": _START_TIME})


@app.route("/api/all_off", methods=["POST"])
def api_all_off():
    for dev_id in db.list_devices_dict():
        set_device(dev_id, False)
    return jsonify({"message": "Tüm cihazlar kapatıldı."})


# ── WiFi / Ağ yönetimi ───────────────────────────────────────────────────────
_WIFI_IFACE = "wlan0"


def _run_nmcli(*args, timeout=15) -> tuple[bool, str]:
    r = subprocess.run(["nmcli"] + list(args), capture_output=True, text=True, timeout=timeout)
    return r.returncode == 0, (r.stdout + r.stderr).strip()


def _active_connection_name() -> str | None:
    ok, out = _run_nmcli("-t", "-f", "NAME,DEVICE", "con", "show", "--active")
    if not ok:
        return None
    for line in out.splitlines():
        parts = line.split(":")
        if len(parts) >= 2 and parts[-1] == _WIFI_IFACE:
            return ":".join(parts[:-1])
    return None


def _current_ip() -> str:
    try:
        r = subprocess.run(
            ["ip", "-j", "addr", "show", _WIFI_IFACE],
            capture_output=True, text=True, timeout=5,
        )
        data = json.loads(r.stdout)
        for iface in data:
            for addr in iface.get("addr_info", []):
                if addr.get("family") == "inet":
                    return addr["local"]
    except Exception:
        pass
    return ""


@app.route("/api/wifi/status")
def api_wifi_status():
    ok, out = _run_nmcli("-t", "-f", "ACTIVE,SSID,SIGNAL,SECURITY", "dev", "wifi", "list")
    ssid, signal_pct, connected = "", 0, False
    if ok:
        for line in out.splitlines():
            parts = line.split(":")
            if len(parts) >= 4 and parts[0] == "yes":
                connected = True
                ssid = parts[1]
                try:
                    signal_pct = int(parts[2])
                except ValueError:
                    signal_pct = 0
                break
    return jsonify({"ssid": ssid, "signal": signal_pct, "ip": _current_ip(), "connected": connected})


@app.route("/api/wifi/scan")
def api_wifi_scan():
    # rescan requires sudo; sudoers rule added for this exact command
    subprocess.run(["sudo", "nmcli", "device", "wifi", "rescan", "ifname", _WIFI_IFACE],
                   capture_output=True, timeout=10)
    import time as _time; _time.sleep(3)
    ok, out = _run_nmcli("-t", "-f", "ACTIVE,SSID,SIGNAL,SECURITY", "dev", "wifi", "list", timeout=10)
    if not ok:
        return jsonify({"error": out}), 500
    networks = []
    seen: set[str] = set()
    connected_ssid = ""
    for line in out.splitlines():
        parts = line.split(":")
        if len(parts) < 4:
            continue
        if parts[0] == "yes":
            connected_ssid = parts[1]
            break
    for line in out.splitlines():
        parts = line.split(":")
        if len(parts) < 4:
            continue
        active, ssid, sig_str, security = parts[0], parts[1], parts[2], ":".join(parts[3:])
        if not ssid or ssid in seen:
            continue
        seen.add(ssid)
        try:
            sig = int(sig_str)
        except ValueError:
            sig = 0
        networks.append({
            "ssid": ssid,
            "signal": sig,
            "security": bool(security.strip()),
            "connected": ssid == connected_ssid,
        })
    networks.sort(key=lambda n: -n["signal"])
    return jsonify(networks)


@app.route("/api/wifi/connect", methods=["POST"])
def api_wifi_connect():
    data = request.get_json(silent=True) or {}
    ssid = (data.get("ssid") or "").strip()
    password = (data.get("password") or "").strip()
    if not ssid:
        return jsonify({"ok": False, "message": "SSID gerekli"}), 400
    args = ["sudo", "nmcli", "dev", "wifi", "connect", ssid]
    if password:
        args += ["password", password]
    r = subprocess.run(args, capture_output=True, text=True, timeout=30)
    ok, out = r.returncode == 0, (r.stdout + r.stderr).strip()
    if ok:
        return jsonify({"ok": True, "message": f"'{ssid}' ağına bağlanıldı.", "ip": _current_ip()})
    return jsonify({"ok": False, "message": out}), 500


@app.route("/api/wifi/ip-config", methods=["POST"])
def api_wifi_ip_config():
    data = request.get_json(silent=True) or {}
    use_custom = bool(data.get("use_custom", False))
    ip = (data.get("ip") or "").strip()

    if use_custom and not re.match(r"^\d{1,3}(\.\d{1,3}){3}$", ip):
        return jsonify({"ok": False, "message": "Geçersiz IP adresi"}), 400

    con = _active_connection_name()
    if not con:
        return jsonify({"ok": False, "message": "Aktif WiFi bağlantısı bulunamadı"}), 500

    def _sudo_nmcli(*args, timeout=15):
        r = subprocess.run(["sudo", "nmcli"] + list(args), capture_output=True, text=True, timeout=timeout)
        return r.returncode == 0, (r.stdout + r.stderr).strip()

    # Özel IP kapalıyken de DHCP değil, varsayılan statik 192.168.1.200 uygulanır
    target_ip = ip if use_custom else "192.168.1.200"
    ok, out = _sudo_nmcli("con", "mod", con,
                          "ipv4.method", "manual",
                          "ipv4.addresses", f"{target_ip}/24",
                          "ipv4.gateway", "192.168.1.1",
                          "ipv4.dns", "8.8.8.8")
    if not ok:
        return jsonify({"ok": False, "message": out}), 500

    ok2, out2 = _sudo_nmcli("con", "up", con, timeout=20)
    new_ip = target_ip
    if ok2:
        return jsonify({"ok": True, "message": "IP ayarları uygulandı.", "new_ip": new_ip})
    return jsonify({"ok": False, "message": out2}), 500


@app.route("/api/system/restart", methods=["POST"])
def api_system_restart():
    threading.Timer(1.0, lambda: os.kill(os.getpid(), signal.SIGTERM)).start()
    return jsonify({"ok": True, "message": "Yeniden başlatılıyor…"})


# ── Otomasyon API rotaları ────────────────────────────────────────────────────
@app.route("/api/automations", methods=["GET"])
def api_automations_list():
    return jsonify(db.get_automations())


@app.route("/api/automations", methods=["POST"])
def api_automations_create():
    data = request.get_json(silent=True) or {}
    required = ("device_id", "action", "hour", "minute")
    for f in required:
        if f not in data:
            return jsonify({"error": f"{f} gerekli"}), 400
    if data["action"] not in ("on", "off"):
        return jsonify({"error": "action 'on' veya 'off' olmalı"}), 400
    if not db.get_device(data["device_id"]):
        return jsonify({"error": "Cihaz bulunamadı"}), 404
    data.setdefault("name", "Otomasyon")
    data.setdefault("days", list(range(7)))
    auto_id = db.create_automation(data)
    _add_scheduler_job(auto_id)
    return jsonify(db.get_automation(auto_id)), 201


@app.route("/api/automations/<auto_id>", methods=["PATCH"])
def api_automations_patch(auto_id: str):
    if not db.get_automation(auto_id):
        return jsonify({"error": "Otomasyon bulunamadı"}), 404
    data = request.get_json(silent=True) or {}
    db.update_automation(auto_id, data)
    _remove_scheduler_job(auto_id)
    _add_scheduler_job(auto_id)
    return jsonify(db.get_automation(auto_id))


@app.route("/api/automations/<auto_id>", methods=["DELETE"])
def api_automations_delete(auto_id: str):
    if not db.get_automation(auto_id):
        return jsonify({"error": "Otomasyon bulunamadı"}), 404
    _remove_scheduler_job(auto_id)
    db.delete_automation(auto_id)
    return jsonify({"ok": True})


# ── Akıllı Öneriler ───────────────────────────────────────────────────────────
_WEEKDAY_NAMES = ["Pazartesi", "Salı", "Çarşamba", "Perşembe", "Cuma", "Cumartesi", "Pazar"]


def _llm_complete(prompt: str) -> str:
    """Metni önce Gemini (generateContent) ile, başarısız olursa Ollama ile tamamlar.
    Gemini metin API'si Live API'den ayrı kotaya sahiptir."""
    # 1) Gemini metin modeli
    client = _get_genai_client()
    if client is not None:
        try:
            r = client.models.generate_content(model=GEMINI_TEXT_MODEL, contents=prompt)
            text = (r.text or "").strip()
            if text:
                return text
        except Exception as e:
            log.warning("Gemini metin analizi başarısız, Ollama'ya geçiliyor: %s", str(e)[:160])
    # 2) Ollama fallback
    try:
        r = requests.post(
            f"{OLLAMA_HOST}/api/chat",
            json={"model": OLLAMA_MODEL, "messages": [{"role": "user", "content": prompt}], "stream": False},
            timeout=120,
        )
        r.raise_for_status()
        return ((r.json().get("message") or {}).get("content") or "").strip()
    except Exception as e:
        log.warning("Ollama metin analizi başarısız: %s", str(e)[:160])
        return ""


def _analyze_suggestions() -> int:
    """Son 14 günün işlem geçmişini AI (Gemini, yedek Ollama) ile analiz edip öneri üretir.
    Üretilen yeni öneri sayısını döndürür."""
    from datetime import datetime as _dt, timedelta as _td, timezone as _tz

    devices = db.list_devices_dict()
    if not devices:
        return 0

    since = (_dt.now(_tz.utc) - _td(days=14)).isoformat()
    logs = db.get_action_log(since_iso=since, exclude_sources=["automation"])
    if len(logs) < 3:
        log.info("Öneri analizi: yetersiz veri (%d kayıt)", len(logs))
        return 0

    # Kompakt özet: cihaz etiketi, aksiyon, saat:dakika, haftanın günü
    lines = []
    for entry in logs:
        dev = devices.get(entry["device_id"])
        if not dev:
            continue
        try:
            ts = _dt.fromisoformat(entry["created_at"])
        except ValueError:
            continue
        wd = _WEEKDAY_NAMES[ts.weekday()]
        lines.append(f'{dev["label"]} | {entry["action"]} | {ts.hour:02d}:{ts.minute:02d} | {wd}')

    if not lines:
        return 0

    device_list = [{"id": did, "label": d["label"]} for did, d in devices.items()]
    prompt = (
        "Sen bir akıllı ev otomasyon asistanısın. Aşağıda kullanıcının son 2 haftadaki cihaz "
        "işlem geçmişi var (cihaz | aksiyon | saat | gün). Tekrar eden rutinleri bul (örn. her "
        "sabah belirli saatte ışık açma). SADECE en az 3 kez tekrarlanan güçlü rutinleri öner. "
        "Sadece JSON dizisi döndür, başka metin yazma.\n\n"
        f"Cihazlar: {json.dumps(device_list, ensure_ascii=False)}\n\n"
        "İşlem geçmişi:\n" + "\n".join(lines) + "\n\n"
        'Çıktı formatı: [{"device_id":"<id>","action":"on|off","hour":<0-23>,"minute":<0-59>,'
        '"days":[0-6 arası, 0=Pazartesi],"reason":"kısa Türkçe gerekçe"}]\n'
        "Rutin yoksa boş dizi döndür: []"
    )
    content = _llm_complete(prompt)
    if not content:
        log.warning("Öneri analizi: LLM yanıtı boş")
        return 0
    try:
        m = re.search(r"\[[\s\S]*\]", content)
        if m:
            content = m.group(0)
        arr = json.loads(content)
    except Exception as e:
        log.warning("Öneri analizi JSON ayrıştırma hatası: %s", e)
        return 0

    if not isinstance(arr, list):
        return 0

    existing_autos = db.get_automations()
    existing_sugs = db.get_suggestions("pending")

    def _is_dup(did: str, action: str, hour: int) -> bool:
        for a in existing_autos:
            if a["device_id"] == did and a["action"] == action and a["hour"] == hour:
                return True
        for s in existing_sugs:
            if s["device_id"] == did and s["action"] == action and s["hour"] == hour:
                return True
        return False

    created = 0
    for item in arr:
        try:
            did = item["device_id"]
            action = item["action"]
            hour = int(item["hour"])
            minute = int(item["minute"])
        except (KeyError, ValueError, TypeError):
            continue
        if did not in devices or action not in ("on", "off"):
            continue
        if not (0 <= hour <= 23 and 0 <= minute <= 59):
            continue
        if _is_dup(did, action, hour):
            continue
        days = item.get("days", list(range(7)))
        if not isinstance(days, list) or not days:
            days = list(range(7))
        days = [int(d) for d in days if isinstance(d, (int, float)) and 0 <= int(d) <= 6]
        db.create_suggestion({
            "device_id": did, "action": action, "hour": hour, "minute": minute,
            "days": days or list(range(7)), "reason": str(item.get("reason", "")).strip(),
        })
        existing_sugs.append({"device_id": did, "action": action, "hour": hour})
        created += 1

    # Eski logları buda (30 günden eski)
    try:
        prune_before = (_dt.now(_tz.utc) - _td(days=30)).isoformat()
        db.prune_action_log(prune_before)
    except Exception:
        pass

    log.info("Öneri analizi tamamlandı: %d yeni öneri", created)
    return created


def _analyze_suggestions_job() -> None:
    try:
        _analyze_suggestions()
    except Exception as e:
        log.exception("Öneri analiz job hatası: %s", e)


@app.route("/api/suggestions", methods=["GET"])
def api_suggestions_list():
    devices = db.list_devices_dict()
    out = []
    for s in db.get_suggestions("pending"):
        dev = devices.get(s["device_id"])
        out.append({**s, "device_label": dev["label"] if dev else s["device_id"],
                    "device_icon": dev["icon"] if dev else "🏠"})
    return jsonify(out)


@app.route("/api/suggestions/analyze", methods=["POST"])
def api_suggestions_analyze():
    count = _analyze_suggestions()
    return jsonify({"ok": True, "created": count})


@app.route("/api/suggestions/<sug_id>/accept", methods=["POST"])
def api_suggestions_accept(sug_id: str):
    sug = db.get_suggestion(sug_id)
    if not sug:
        return jsonify({"error": "Öneri bulunamadı"}), 404
    dev = db.get_device(sug["device_id"])
    name = f'{dev["label"] if dev else "Cihaz"} otomasyonu'
    auto_id = db.create_automation({
        "name": name, "device_id": sug["device_id"], "action": sug["action"],
        "hour": sug["hour"], "minute": sug["minute"], "days": sug["days"],
    })
    _add_scheduler_job(auto_id)
    db.delete_suggestion(sug_id)
    return jsonify({"ok": True, "automation": db.get_automation(auto_id)}), 201


@app.route("/api/suggestions/<sug_id>", methods=["DELETE"])
def api_suggestions_delete(sug_id: str):
    if not db.get_suggestion(sug_id):
        return jsonify({"error": "Öneri bulunamadı"}), 404
    db.delete_suggestion(sug_id)
    return jsonify({"ok": True})


# ── Ayarlar ───────────────────────────────────────────────────────────────────
@app.route("/api/settings", methods=["GET"])
def api_settings_get():
    return jsonify({"gemini_key_set": bool(GEMINI_API_KEY)})


@app.route("/api/settings/gemini-key", methods=["POST"])
def api_settings_gemini_key():
    global GEMINI_API_KEY, _genai_client
    data = request.get_json(silent=True) or {}
    key = (data.get("key") or "").strip()
    if not key:
        return jsonify({"ok": False, "error": "Anahtar boş olamaz"}), 400
    db.set_setting("gemini_api_key", key)
    GEMINI_API_KEY = key
    _genai_client = None  # sonraki bağlantı yeni anahtarla kurulur
    return jsonify({"ok": True, "gemini_key_set": True})


# ── APScheduler ───────────────────────────────────────────────────────────────
_scheduler = None

_DAY_NAMES = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]


def _automation_job(device_id: str, action: str) -> None:
    state = action == "on"
    ok = set_device(device_id, state, source="automation")
    log.info("Otomasyon tetiklendi: device=%s action=%s ok=%s", device_id, action, ok)


def _aps_day_str(days: list) -> str:
    return ",".join(_DAY_NAMES[d % 7] for d in sorted(set(days)))


def _add_scheduler_job(auto_id: str) -> None:
    if _scheduler is None:
        return
    auto = db.get_automation(auto_id)
    if not auto or not auto["enabled"]:
        return
    days_str = _aps_day_str(auto["days"])
    try:
        _scheduler.add_job(
            _automation_job,
            _CronTrigger(hour=auto["hour"], minute=auto["minute"], day_of_week=days_str),
            args=[auto["device_id"], auto["action"]],
            id=f"auto_{auto_id}",
            replace_existing=True,
        )
        log.info("Scheduler job eklendi: %s @ %02d:%02d [%s]", auto["name"], auto["hour"], auto["minute"], days_str)
    except Exception as e:
        log.warning("Scheduler job eklenemedi: %s", e)


def _remove_scheduler_job(auto_id: str) -> None:
    if _scheduler is None:
        return
    try:
        _scheduler.remove_job(f"auto_{auto_id}")
    except Exception:
        pass


def _init_scheduler() -> None:
    global _scheduler
    if not APS_AVAILABLE:
        log.warning("APScheduler kurulu değil — otomasyonlar çalışmayacak")
        return
    _scheduler = _APSched()
    _scheduler.start()
    for auto in db.get_automations():
        if auto["enabled"]:
            _add_scheduler_job(auto["id"])
    # Günlük akıllı öneri analizi (her gün 03:00)
    try:
        _scheduler.add_job(
            _analyze_suggestions_job,
            _CronTrigger(hour=3, minute=0),
            id="daily_suggestions",
            replace_existing=True,
        )
    except Exception as e:
        log.warning("Öneri analiz job eklenemedi: %s", e)
    log.info("APScheduler başlatıldı, %d otomasyon yüklendi.", len([a for a in db.get_automations() if a["enabled"]]))


# ── Gemini Live Tool Executor ─────────────────────────────────────────────────
def _execute_gemini_tool(name: str, args: dict) -> dict:
    if name == "toggle_device":
        device_id = args.get("device_id")
        state = args.get("state")
        if not device_id or state is None:
            return {"success": False, "error": "Geçersiz parametre"}
        ok = set_device(device_id, bool(state), source="chat")
        dev = db.get_device(device_id)
        return {"success": ok, "device_id": device_id, "state": bool(state), "label": dev["label"] if dev else device_id}

    if name == "get_device_status":
        devices = db.list_devices_dict()
        return {"devices": [{"id": did, "label": d["label"], "state": d["state"]} for did, d in devices.items()]}

    if name == "turn_all_off":
        for dev_id in db.list_devices_dict():
            set_device(dev_id, False, source="chat")
        return {"success": True}

    if name == "create_or_update_automation":
        if not db.get_device(args.get("device_id", "")):
            return {"success": False, "error": "Cihaz bulunamadı"}
        try:
            auto_data = {
                "name": args.get("name", "Otomasyon"),
                "device_id": args["device_id"],
                "action": args["action"],
                "hour": int(args["hour"]),
                "minute": int(args["minute"]),
                "days": [int(d) for d in args.get("days", list(range(7)))],
            }
            auto_id = db.create_automation(auto_data)
            _add_scheduler_job(auto_id)
            return {"success": True, "automation_id": auto_id, "name": auto_data["name"]}
        except Exception as e:
            return {"success": False, "error": str(e)}

    return {"success": False, "error": f"Bilinmeyen tool: {name}"}


# ── Gemini Live API WebSocket ─────────────────────────────────────────────────
async def _gemini_live_session(ws) -> None:
    if not GENAI_AVAILABLE:
        ws.send(json.dumps({"type": "error", "message": "google-genai paketi kurulu değil. pip install google-genai"}))
        return

    api_key = GEMINI_API_KEY
    if not api_key:
        ws.send(json.dumps({"type": "error", "message": "GEMINI_API_KEY ortam değişkeni ayarlanmamış."}))
        return

    from google.genai import types as gtypes

    devices = db.list_devices_dict()
    device_list_str = json.dumps(
        [{"id": did, "label": d["label"]} for did, d in devices.items()],
        ensure_ascii=False,
    )
    system_prompt = (
        "Sen Türkçe konuşan bir akıllı ev asistanısın. "
        "Kullanıcının sesli komutlarını anlayarak akıllı ev cihazlarını kontrol et ve "
        "otomasyon kurmasına yardımcı ol. "
        f"Mevcut cihazlar: {device_list_str}. "
        "Her zaman kısa ve net Türkçe yanıt ver. "
        "Konuşma geçmişini hatırla ve konuşmaya devam et. "
        "Hava durumu, haberler veya güncel bilgi gibi soruları Google arama ile yanıtlayabilirsin."
    )

    tools = [gtypes.Tool(function_declarations=[
        gtypes.FunctionDeclaration(
            name="toggle_device",
            description="Bir cihazı aç veya kapat",
            parameters=gtypes.Schema(
                type=gtypes.Type.OBJECT,
                properties={
                    "device_id": gtypes.Schema(type=gtypes.Type.STRING, description="Cihaz ID'si"),
                    "state": gtypes.Schema(type=gtypes.Type.BOOLEAN, description="True=aç, False=kapat"),
                },
                required=["device_id", "state"],
            ),
        ),
        gtypes.FunctionDeclaration(
            name="get_device_status",
            description="Tüm cihazların mevcut durumunu listele",
            parameters=gtypes.Schema(type=gtypes.Type.OBJECT, properties={}),
        ),
        gtypes.FunctionDeclaration(
            name="turn_all_off",
            description="Tüm cihazları kapat",
            parameters=gtypes.Schema(type=gtypes.Type.OBJECT, properties={}),
        ),
        gtypes.FunctionDeclaration(
            name="create_or_update_automation",
            description="Belirli bir saatte cihazı otomatik aç veya kapat",
            parameters=gtypes.Schema(
                type=gtypes.Type.OBJECT,
                properties={
                    "name": gtypes.Schema(type=gtypes.Type.STRING, description="Otomasyon adı"),
                    "device_id": gtypes.Schema(type=gtypes.Type.STRING, description="Cihaz ID'si"),
                    "action": gtypes.Schema(type=gtypes.Type.STRING, enum=["on", "off"]),
                    "hour": gtypes.Schema(type=gtypes.Type.INTEGER, description="Saat (0-23)"),
                    "minute": gtypes.Schema(type=gtypes.Type.INTEGER, description="Dakika (0-59)"),
                    "days": gtypes.Schema(
                        type=gtypes.Type.ARRAY,
                        items=gtypes.Schema(type=gtypes.Type.INTEGER),
                        description="Günler: 0=Pazartesi … 6=Pazar",
                    ),
                },
                required=["name", "device_id", "action", "hour", "minute", "days"],
            ),
        ),
    ])]

    config = gtypes.LiveConnectConfig(
        response_modalities=["AUDIO"],
        system_instruction=system_prompt,
        tools=tools,
    )

    client = _get_genai_client()
    loop = asyncio.get_event_loop()

    # Queue for browser→Gemini messages; shared across reconnects
    audio_queue: asyncio.Queue = asyncio.Queue(maxsize=200)
    ws_closed = asyncio.Event()

    async def read_ws_loop():
        """Read browser WebSocket messages into queue; set ws_closed when done."""
        while True:
            try:
                msg = await loop.run_in_executor(None, ws.receive)
            except Exception:
                break
            if msg is None:
                break
            try:
                await audio_queue.put(msg)
            except Exception:
                break
        ws_closed.set()

    read_task = asyncio.create_task(read_ws_loop())
    connected_sent = False

    try:
        log.info("Gemini Live bağlantısı kuruluyor: %s", GEMINI_MODEL)

        while not ws_closed.is_set():
            log.info("Gemini Live session başlatılıyor")
            try:
                async with client.aio.live.connect(model=GEMINI_MODEL, config=config) as session:
                    log.info("Gemini Live bağlandı")
                    if not connected_sent:
                        connected_sent = True
                        try:
                            ws.send(json.dumps({"type": "connected"}))
                        except Exception:
                            return

                    async def send_to_gemini():
                        while not ws_closed.is_set():
                            try:
                                msg = await asyncio.wait_for(audio_queue.get(), timeout=1.0)
                            except asyncio.TimeoutError:
                                continue
                            except asyncio.CancelledError:
                                return
                            try:
                                data = json.loads(msg)
                                msg_type = data.get("type")
                                if msg_type == "audio":
                                    audio_bytes = base64.b64decode(data["data"])
                                    await session.send_realtime_input(
                                        audio=gtypes.Blob(data=audio_bytes, mime_type="audio/pcm;rate=16000")
                                    )
                                elif msg_type == "end_of_turn":
                                    await session.send_client_content(
                                        turns=gtypes.Content(role="user", parts=[gtypes.Part(text=" ")]),
                                        turn_complete=True,
                                    )
                            except asyncio.CancelledError:
                                return
                            except Exception as e:
                                log.warning("Live send_to_gemini hata: %s", e)

                    def _send_state(value):
                        if _send_state.last != value:
                            _send_state.last = value
                            try:
                                ws.send(json.dumps({"type": "state", "value": value}))
                            except Exception:
                                pass
                    _send_state.last = None

                    async def recv_from_gemini():
                        turn_has_audio = False
                        async for response in session.receive():
                            try:
                                sc = response.server_content
                                if sc and sc.model_turn:
                                    has_audio = False
                                    for part in sc.model_turn.parts:
                                        if part.inline_data and part.inline_data.data:
                                            has_audio = True
                                            turn_has_audio = True
                                            _send_state("speaking")
                                            ws.send(json.dumps({
                                                "type": "audio",
                                                "data": base64.b64encode(part.inline_data.data).decode(),
                                            }))
                                    # Model cevap üretiyor ama bu pakette ses yok
                                    if not has_audio:
                                        _send_state("processing")
                                if sc and getattr(sc, "turn_complete", False):
                                    turn_has_audio = False
                                    _send_state("listening")
                                if response.tool_call:
                                    _send_state("processing")
                                    for fc in response.tool_call.function_calls:
                                        result = _execute_gemini_tool(fc.name, dict(fc.args))
                                        try:
                                            ws.send(json.dumps({"type": "tool_result", "tool": fc.name, "result": result}))
                                        except Exception:
                                            pass
                                        await session.send_tool_response(
                                            function_responses=[gtypes.FunctionResponse(
                                                id=fc.id,
                                                name=fc.name,
                                                response={"result": json.dumps(result, ensure_ascii=False)},
                                            )]
                                        )
                            except asyncio.CancelledError:
                                return
                            except Exception as e:
                                log.warning("Live recv_from_gemini hata: %s", e)

                    send_task = asyncio.create_task(send_to_gemini())
                    recv_task = asyncio.create_task(recv_from_gemini())
                    done, pending = await asyncio.wait(
                        [send_task, recv_task, read_task],
                        return_when=asyncio.FIRST_COMPLETED,
                    )
                    for t in pending - {read_task}:
                        t.cancel()
                        try:
                            await t
                        except asyncio.CancelledError:
                            pass

                    if read_task in done or ws_closed.is_set():
                        break
                    # recv_task finished → Gemini closed this session turn; reconnect
                    log.info("Gemini session kapandı, yeniden bağlanılıyor...")
                    await asyncio.sleep(0.3)
            except Exception as e:
                log.warning("Gemini Live session hatası: %s", e)
                if ws_closed.is_set():
                    break
                emsg = str(e).lower()
                # Ölümcül hatalar — yeniden denemenin anlamı yok
                if any(k in emsg for k in ("quota", "1011", "api key", "api_key",
                                            "permission", "invalid", "unauthor", "403", "429")):
                    user_msg = ("Gemini API kotanız dolmuş veya anahtarınız geçersiz. "
                                "Ayarlar sekmesinden geçerli bir API anahtarı girin."
                                if "quota" in emsg or "1011" in emsg or "429" in emsg
                                else "Gemini API anahtarı geçersiz. Ayarlar'dan kontrol edin.")
                    try:
                        ws.send(json.dumps({"type": "error", "message": user_msg}))
                    except Exception:
                        pass
                    break
                await asyncio.sleep(1.0)
    finally:
        ws_closed.set()
        read_task.cancel()
        try:
            await read_task
        except asyncio.CancelledError:
            pass


if SOCK_AVAILABLE:
    @sock.route("/ws/live")
    def ws_live(ws):
        log.info("Live WS bağlantısı açıldı")
        try:
            asyncio.run(_gemini_live_session(ws))
        except Exception as e:
            log.exception("Live session başlatılamadı: %s", e)
            try:
                ws.send(json.dumps({"type": "error", "message": str(e)}))
            except Exception:
                pass
        log.info("Live WS bağlantısı kapandı")


# ── Gemini client singleton ───────────────────────────────────────────────────
_genai_client = None


def _get_genai_client():
    global _genai_client
    if _genai_client is None and GENAI_AVAILABLE and GEMINI_API_KEY:
        import google.genai as genai
        _genai_client = genai.Client(api_key=GEMINI_API_KEY)
        log.info("Gemini client hazırlandı")
    return _genai_client


# ── DB + GPIO + MQTT init ────────────────────────────────────────────────────
db.init_db()
_load_gemini_key_from_db()
gpio_init_all_devices()
_init_mqtt()
_init_scheduler()
_get_genai_client()
# Whisper ve TTS arka planda yüklenir — Flask hemen cevap vermeye başlar
def _bg_startup():
    get_whisper_model()
    for _dev in db.list_devices_dict().values():
        prewarm_tts(_dev["label"])
threading.Thread(target=_bg_startup, daemon=True).start()


# ── Entry Point ───────────────────────────────────────────────────────────────
if __name__ == "__main__":
    try:
        app.run(host="0.0.0.0", port=PORT, debug=False, threaded=True)
    finally:
        if USE_GPIO:
            if _lgpio_handle is not None:
                import lgpio as _lgpio_mod
                _lgpio_mod.gpiochip_close(_lgpio_handle)
            else:
                import RPi.GPIO as _rpigpio_mod
                _rpigpio_mod.cleanup()
