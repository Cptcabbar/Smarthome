# Smarthome — Raspberry Pi ile Akıllı Ev Kontrolü

Bu, bir Raspberry Pi 5 üzerinde çalışan, dokunmatik ekranlı bir akıllı ev kontrol panelim. GPIO pinleri, MQTT üzerinden ESP8266/NodeMCU cihazları ve sesli komutlarla evdeki cihazları yönetebiliyorsun. Türkçe konuşmayı yerelde (Whisper ile) anlıyor, istersen Gemini Live ile gerçek bir sesli asistana da bağlanabiliyor. Bitirme projem olarak geliştirdim.

**Depo:** [github.com/Cptcabbar/Smarthome](https://github.com/Cptcabbar/Smarthome)

## Neler yapabiliyor

- **Web arayüzü** — cihaz ekleme, açma/kapama, tema seçimi, otomasyon zamanlayıcıları
- **GPIO (lgpio)** — Pi 5 üzerinden doğrudan röle/LED kontrolü
- **MQTT** — uzaktaki NodeMCU/ESP8266 cihazlarıyla haberleşme (`mqtt_topic` üzerinden)
- **Sesli komut** — `faster-whisper` ile yerelde konuşma tanıma, cihaz adlarını fuzzy eşleştirmeyle buluyor
- **Ollama** — yeni bir cihaz eklediğinde ona otomatik sesli komut anahtar kelimeleri üretiyor
- **Gemini Live (isteğe bağlı)** — canlı sesli asistan; anahtar sadece sunucuda tutuluyor
- **gTTS** — cihaz geri bildirimlerini önbelleğe alınmış sesle veriyor
- **Kiosk modu** — `kiosk.sh` / `splash.html` ile tam ekran dokunmatik kullanım

## Donanım

| Bileşen | Görevi |
|---|---|
| Raspberry Pi 5 | ana sunucu, GPIO, web arayüzü |
| NodeMCU / ESP8266 | MQTT üzerinden röle, opsiyonel servo |
| Dokunmatik ekran | kiosk arayüzü |

ESP8266 tarafının firmware'i (röle + servo kontrolü) ayrı bir Arduino sketch paketi olarak paylaşılıyor: [ESP8266 NodeMCU — Röle ve Servo Kontrol Firmware](https://github.com/user-attachments/files/28077062/Nodemcu.role.zip).

## Kurulum

```bash
git clone https://github.com/Cptcabbar/Smarthome.git
cd Smarthome
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt

cp .env.example .env
# .env içine kendi GEMINI_API_KEY'ini yaz, ya da arayüzden Ayarlar → Gemini anahtarı gir

./run.sh
```

Tarayıcıdan `http://<pi-ip>:3000` adresine gidince arayüz açılıyor.

### Gerekenler

- Python 3.11+
- `ffmpeg` (Whisper için)
- İsteğe bağlı: [Ollama](https://ollama.com) (`qwen2.5:1.5b` gibi küçük bir model yeterli)
- İsteğe bağlı: bir MQTT broker (Mosquitto gibi)
- Gemini kullanmak istersen [Google AI Studio](https://aistudio.google.com/apikey)'dan bir API anahtarı

## API anahtarı ve gizlilik

Anahtarın repoya sızmaması için şu kurallara dikkat ediyorum:

| Kaynak | Git'e gidiyor mu? |
|---|---|
| `.env` | Hayır — `.gitignore`'da |
| `smarthome.db` | Hayır — ayarlardaki Gemini anahtarı da burada saklanabiliyor |
| Arayüz → Ayarlar | Anahtar sadece sunucuya POST ediliyor, repoya yazılmıyor |

Gerçek bir anahtarı asla commit etme. Sızma şüphesi varsa anahtarı Google tarafında hemen iptal edip yenisini oluştur.

Örnek ortam dosyası: `.env.example`.

## Ortam değişkenleri

| Değişken | Ne işe yarar | Varsayılan |
|---|---|---|
| `GEMINI_API_KEY` | Gemini API anahtarı | (boş) |
| `PORT` | HTTP portu | `3000` |
| `MQTT_HOST` / `MQTT_PORT` | MQTT broker adresi | `127.0.0.1` / `1883` |
| `OLLAMA_HOST` / `OLLAMA_MODEL` | anahtar kelime üretimi için | `http://127.0.0.1:11434` / `qwen2.5:1.5b` |
| `WHISPER_*` | STT modeli ve cihazı | `base`, `cpu`, `int8` |
| `SMARTHOME_DB` | SQLite dosya yolu | `./smarthome.db` |

## Proje yapısı

```
app.py             # Flask API, GPIO, MQTT, ses, Gemini WS
db.py              # SQLite (cihazlar, otomasyon, ayarlar)
templates/         # Web arayüzü
run.sh             # .env'i yükleyip sunucuyu başlatır
kiosk.sh           # Chromium kiosk modu
requirements.txt
```

## Lisans

Şu an için ayrı bir lisans dosyası yok; kullanmak/uyarlamak istersen önce benimle iletişime geç.
