#!/usr/bin/env python3
"""
Smart Home Control System
Raspberry Pi 5 - Flask + faster-whisper + SQLite + Ollama keyword üretimi
"""

from __future__ import annotations

import json
import logging
import os
import re
import tempfile
import threading
from difflib import SequenceMatcher
from pathlib import Path

import paho.mqtt.client as paho_mqtt
import requests
from flask import Flask, jsonify, render_template, request

import db

# ── Logging ──────────────────────────────────────────────────────────────────
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger(__name__)

# ── App ───────────────────────────────────────────────────────────────────────
app = Flask(__name__)

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
            import subprocess
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
def set_device(device_id: str, state: bool) -> bool:
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
            set_device(device_id, new_state)
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


@app.route("/api/all_off", methods=["POST"])
def api_all_off():
    for dev_id in db.list_devices_dict():
        set_device(dev_id, False)
    return jsonify({"message": "Tüm cihazlar kapatıldı."})


# ── DB + GPIO + MQTT init ────────────────────────────────────────────────────
db.init_db()
gpio_init_all_devices()
_init_mqtt()
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
