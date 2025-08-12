cat > install_browser.sh <<'EOF'
#!/usr/bin/env bash
set -euo pipefail

echo "==> Installing browser for Selenium on Ubuntu 22.04"

# Base libs often needed by (headless) Chrome/Chromium
echo "==> Installing base libraries…"
sudo apt update
sudo apt install -y wget curl gnupg ca-certificates apt-transport-https \
  fonts-liberation libnss3 libxss1 libasound2 libgbm1 libu2f-udev xdg-utils

ARCH="$(dpkg --print-architecture)"
echo "==> Architecture: $ARCH"

install_chrome_amd64() {
  echo "==> Setting up Google Chrome APT repo…"
  sudo install -m 0755 -d /etc/apt/keyrings
  if [ ! -f /etc/apt/keyrings/google.gpg ]; then
    wget -qO- https://dl.google.com/linux/linux_signing_key.pub \
      | sudo gpg --dearmor -o /etc/apt/keyrings/google.gpg
    sudo chmod a+r /etc/apt/keyrings/google.gpg
  fi
  echo "deb [arch=amd64 signed-by=/etc/apt/keyrings/google.gpg] http://dl.google.com/linux/chrome/deb/ stable main" \
    | sudo tee /etc/apt/sources.list.d/google-chrome.list >/dev/null

  echo "==> Installing Google Chrome…"
  sudo apt update
  if ! sudo apt install -y google-chrome-stable; then
    echo "!! apt install failed — trying direct .deb"
    wget -q https://dl.google.com/linux/direct/google-chrome-stable_current_amd64.deb -O /tmp/chrome.deb
    sudo apt install -y /tmp/chrome.deb
  fi
}

install_chromium_any() {
  echo "==> Installing Chromium (snap)…"
  if ! command -v snap >/dev/null 2>&1; then
    sudo apt update && sudo apt install -y snapd
  fi
  sudo snap install chromium || true
  # Try apt as a fallback (may be transitional to snap on Jammy)
  if ! command -v /snap/bin/chromium >/dev/null 2>&1 && ! command -v chromium >/dev/null 2>&1; then
    sudo apt install -y chromium-browser || true
  fi
}

if [ "$ARCH" = "amd64" ]; then
  install_chrome_amd64
else
  echo "==> Non-amd64 detected; installing Chromium."
  install_chromium_any
fi

detect_browser_bin() {
  for c in google-chrome google-chrome-stable chromium chromium-browser /snap/bin/chromium /opt/google/chrome/chrome; do
    if command -v "$c" >/dev/null 2>&1; then
      command -v "$c"; return
    elif [ -x "$c" ]; then
      echo "$c"; return
    fi
  done
  echo ""
}

BIN="$(detect_browser_bin)"
if [ -z "$BIN" ]; then
  echo "ERROR: Browser binary not found. Please install google-chrome-stable or chromium manually."
  exit 1
fi

echo "==> Browser installed at: $BIN"
"$BIN" --version || true

cat <<TIP

Done ✅
If your app uses Selenium and expects CHROME_BIN, add this to your .env:
  CHROME_BIN=$BIN

TIP
EOF

chmod +x install_browser.sh
