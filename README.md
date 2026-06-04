# Smarthome — Raspberry Pi Akıllı Ev Kontrolü

Raspberry Pi 5 üzerinde çalışan, dokunmatik kiosk arayüzlü akıllı ev kontrol paneli. GPIO pinleri, MQTT (ESP8266 NodeMCU) ve sesli komutlarla cihazları yönetir; yerel Whisper ile Türkçe konuşmayı anlar, isteğe bağlı Gemini Live ile sesli asistan sunar.

**Depo:** [github.com/Cptcabbar/Smarthome](https://github.com/Cptcabbar/Smarthome)

## Özellikler

- **Web arayüzü** — Cihaz ekleme, aç/kapa, tema, otomasyon zamanlayıcıları
- **GPIO (lgpio)** — Pi 5 üzerinde doğrudan röle / LED kontrolü
- **MQTT** — Uzak NodeMCU / ESP8266 cihazları (`mqtt_topic` ile)
- **Sesli komut** — `faster-whisper` ile yerel STT, fuzzy eşleştirme ile cihaz adı tanıma
- **Ollama** — Yeni cihazlar için otomatik sesli komut anahtar kelimeleri
- **Gemini Live** (isteğe bağlı) — Canlı sesli asistan; anahtar yalnızca sunucuda tutulur
- **gTTS** — Cihaz geri bildirimi için önbellekli ses sentezi
- **Kiosk modu** — `kiosk.sh` / `splash.html` ile tam ekran dokunmatik kullanım

## Donanım

| Bileşen | Rol |
|--------|-----|
| Raspberry Pi 5 | Ana sunucu, GPIO, web UI |
| NodeMCU / ESP8266 | MQTT ile röle, servo (opsiyonel) |
| Dokunmatik ekran | Kiosk arayüzü |

Uzak cihaz firmware paketi: **[ESP8266 NodeMCU — Röle ve Servo Kontrol Firmware](https://github.com/user-attachments/files/28077062/Nodemcu.role.zip)** (Arduino sketch arşivi).

## Kurulum

```bash
git clone https://github.com/Cptcabbar/Smarthome.git
cd Smarthome
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt

cp .env.example .env
# .env içine GEMINI_API_KEY yazın veya arayüzden Ayarlar → Gemini anahtarı girin

./run.sh
```

Tarayıcı: `http://<pi-ip>:3000`

### Gereksinimler

- Python 3.11+
- `ffmpeg` (Whisper için)
- İsteğe bağlı: [Ollama](https://ollama.com) (`qwen2.5:1.5b` veya benzeri küçük model)
- İsteğe bağlı: MQTT broker (ör. Mosquitto)
- Gemini kullanımı için [Google AI Studio](https://aistudio.google.com/apikey) API anahtarı

## API anahtarı ve gizlilik

| Kaynak | Git'e gider mi? |
|--------|------------------|
| `.env` | **Hayır** — `.gitignore` ile hariç tutulur |
| `smarthome.db` | **Hayır** — ayarlar (Gemini anahtarı) burada da saklanabilir |
| Arayüz → Ayarlar | Anahtar yalnızca sunucuya POST edilir; repoya yazılmaz |

**Asla** gerçek anahtarı commit etmeyin. Sızıntı şüphesinde anahtarı Google tarafında **hemen iptal edip yenileyin**.

Örnek ortam dosyası: `.env.example`

## Ortam değişkenleri

| Değişken | Açıklama | Varsayılan |
|----------|----------|------------|
| `GEMINI_API_KEY` | Gemini API | (boş) |
| `PORT` | HTTP portu | `3000` |
| `MQTT_HOST` / `MQTT_PORT` | MQTT broker | `127.0.0.1` / `1883` |
| `OLLAMA_HOST` / `OLLAMA_MODEL` | Keyword üretimi | `http://127.0.0.1:11434` / `qwen2.5:1.5b` |
| `WHISPER_*` | STT model ve cihaz | `base`, `cpu`, `int8` |
| `SMARTHOME_DB` | SQLite yolu | `./smarthome.db` |

## Proje yapısı

```
app.py              # Flask API, GPIO, MQTT, ses, Gemini WS
db.py               # SQLite (cihazlar, otomasyon, ayarlar)
templates/          # Web arayüzü
run.sh              # .env yükleyip sunucuyu başlatır
kiosk.sh            # Chromium kiosk
requirements.txt
```

## Lisans

Bu depo için lisans dosyası yoksa kullanım koşullarını depo sahibiyle netleştirin.
