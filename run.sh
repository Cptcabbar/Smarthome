#!/bin/bash
cd "$(dirname "$0")"

# Çalışan eski instance'ı durdur
OLD=$(pgrep -f "python.*app\.py" 2>/dev/null)
if [ -n "$OLD" ]; then
    echo "Eski process durduruluyor (PID: $OLD)..."
    kill "$OLD"
    sleep 1
fi

# Venv varsa onu kullan (faster-whisper dahil tüm paketler burada)
if [ -f "venv/bin/python" ]; then
    PYTHON="venv/bin/python"
else
    PYTHON="python3"
fi

echo "Sunucu başlatılıyor ($PYTHON) → http://$(hostname -I | awk '{print $1}'):3000"
exec "$PYTHON" app.py
