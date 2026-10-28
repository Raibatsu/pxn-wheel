#!/bin/sh
# Root helper for the pxn-wheel system service.
#   install   copy the code to /usr/local/lib/pxn-wheel (root-owned), write the unit, enable + start
#   start | stop | restart | disable | uninstall
# Run via:  sudo sh install-service.sh install      (the GUI uses pkexec for this)
set -e
LIB=/usr/local/lib/pxn-wheel
UNIT=/etc/systemd/system/pxn-wheeld.service
[ "$(id -u)" = 0 ] || { echo "must run as root" >&2; exit 1; }

case "$1" in
install)
    USER_NAME=${SUDO_USER:-$(id -nu "${PKEXEC_UID:-1000}")}
    USER_HOME=$(getent passwd "$USER_NAME" | cut -d: -f6)
    SRC=$(dirname "$(readlink -f "$0")")
    rm -rf "$LIB"; mkdir -p "$LIB"
    cp -r "$SRC/pxnwheel" "$LIB/pxnwheel"
    cp "$SRC/install-service.sh" "$LIB/service.sh"      # later start/stop use this root-owned copy
    find "$LIB" -name __pycache__ -prune -exec rm -rf {} +
    chown -R root:root "$LIB"; chmod -R go-w "$LIB"
    cat > "$UNIT" <<EOF
[Unit]
Description=PXN V10 Ultra wheel bridge (calibration / remapping / FFB passthrough)

[Service]
WorkingDirectory=$LIB
ExecStart=/usr/bin/python3 -m pxnwheel.daemon
Environment=XDG_CONFIG_HOME=$USER_HOME/.config
Environment=PYTHONDONTWRITEBYTECODE=1
Restart=on-failure
RestartSec=2

[Install]
WantedBy=multi-user.target
EOF
    systemctl daemon-reload
    systemctl enable pxn-wheeld.service
    systemctl restart pxn-wheeld.service
    echo "installed. Restart Steam (and any game) once so they stop using the raw wheel." ;;
start|stop|restart) systemctl "$1" pxn-wheeld.service ;;
disable) systemctl disable --now pxn-wheeld.service ;;
uninstall) systemctl disable --now pxn-wheeld.service 2>/dev/null || true
           rm -f "$UNIT"; rm -rf "$LIB"; systemctl daemon-reload ;;
*) echo "usage: $0 install|start|stop|restart|disable|uninstall" >&2; exit 2 ;;
esac
