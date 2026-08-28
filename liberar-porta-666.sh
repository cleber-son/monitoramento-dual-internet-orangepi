#!/usr/bin/env bash
# Libera a porta 666 para processos sem root e reinicia o netmon.
# Rode com: sudo /home/orangepi/netmon/liberar-porta-666.sh
set -e

if [ "$(id -u)" -ne 0 ]; then
    echo "Precisa de root. Rode:  sudo $0" >&2
    exit 1
fi

echo 'net.ipv4.ip_unprivileged_port_start=666' > /etc/sysctl.d/90-netmon.conf
sysctl -q --system
echo "porta minima sem privilegio agora: $(cat /proc/sys/net/ipv4/ip_unprivileged_port_start)"

if [ -f /home/orangepi/netmon/netmon.pid ]; then
    kill "$(cat /home/orangepi/netmon/netmon.pid)" 2>/dev/null || true
    echo "netmon reiniciando — o cron reergue em ate 1 minuto, ja na porta 666."
fi

echo
echo "Confira em ate 1 min:  curl -s http://127.0.0.1:666/api/health"
