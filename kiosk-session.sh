#!/bin/bash
exec > /tmp/kiosk-session.log 2>&1

. /usr/bin/setup_env

KIOSK_CONF="/home/efe/.config/smarthome-kiosk"
mkdir -p "$KIOSK_CONF/labwc" "$KIOSK_CONF/xdg"

for f in environment rc.xml themerc-override; do
    [ -f "/home/efe/.config/labwc/$f" ] && \
        cp "/home/efe/.config/labwc/$f" "$KIOSK_CONF/labwc/"
done

cat > "$KIOSK_CONF/labwc/autostart" << 'AUTOSTART'
kanshi &
squeekboard &
/home/efe/Desktop/smarthome/kiosk.sh &
AUTOSTART

export XDG_CONFIG_HOME="$KIOSK_CONF"
export XDG_CONFIG_DIRS="$KIOSK_CONF/xdg"

exec labwc
