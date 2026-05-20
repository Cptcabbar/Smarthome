#!/bin/bash
exec > /tmp/kiosk.log 2>&1

# Flask'ın port dinlemeye başlamasını bekle (max 30sn)
for i in $(seq 1 30); do
    curl -s http://localhost:3000/splash > /dev/null 2>&1 && break
    sleep 1
done

rm -rf /tmp/chromium-kiosk

exec chromium \
  --kiosk \
  --noerrdialogs \
  --disable-infobars \
  --no-first-run \
  --disable-translate \
  --overscroll-history-navigation=0 \
  --ozone-platform=wayland \
  --user-data-dir=/tmp/chromium-kiosk \
  --password-store=basic \
  --disable-features=TranslateUI \
  --enable-features=VirtualKeyboard \
  http://localhost:3000/splash
