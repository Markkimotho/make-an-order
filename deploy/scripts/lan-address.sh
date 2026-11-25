#!/usr/bin/env bash
# Print the address the other tills and computers in the shop should open.
# Run it on the server PC: deploy/scripts/lan-address.sh
set -euo pipefail

here="$(cd "$(dirname "$0")/.." && pwd)"
port=80
if [ -f "$here/.env" ]; then
  value="$(grep -E '^APP_PORT=' "$here/.env" | tail -n1 | cut -d= -f2- | tr -d '[:space:]"' || true)"
  [ -n "$value" ] && port="$value"
fi

ip=""
if command -v ip >/dev/null 2>&1; then                       # Linux
  ip="$(ip -4 route get 1.1.1.1 2>/dev/null | awk '{for (i = 1; i < NF; i++) if ($i == "src") {print $(i + 1); exit}}')"
fi
if [ -z "$ip" ] && command -v ipconfig >/dev/null 2>&1; then  # macOS
  for iface in en0 en1 en2; do
    ip="$(ipconfig getifaddr "$iface" 2>/dev/null || true)"
    [ -n "$ip" ] && break
  done
fi
if [ -z "$ip" ] && command -v hostname >/dev/null 2>&1; then
  ip="$(hostname -I 2>/dev/null | awk '{print $1}' || true)"
fi
if [ -z "$ip" ]; then
  echo "Could not find this computer's LAN address. On Windows run: ipconfig" >&2
  echo "and use the IPv4 Address of the Wi-Fi or Ethernet adapter." >&2
  exit 1
fi

if [ "$port" = "80" ]; then url="http://$ip"; else url="http://$ip:$port"; fi
echo "$url"
echo "Open $url on every till and pin it. Give this server a fixed address" >&2
echo "(DHCP reservation on the router) so the address never changes." >&2
