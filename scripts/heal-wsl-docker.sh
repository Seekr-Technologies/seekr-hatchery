#!/usr/bin/env bash
set -euo pipefail

# heal-wsl-docker.sh
# Ensure Docker socket and mount translation are fully active and healthy under WSL.
# Run this script on Windows WSL to set up the WSL + Docker environment properly.

# Determine repo directory (default to current directory, resolve to absolute path)
REPO_DIR="${1:-$(pwd)}"
REPO_DIR="$(cd "$REPO_DIR" && pwd)"

echo "Checking WSL environment..."

# Detect if we are in WSL
if [ ! -f /proc/sys/fs/binfmt_misc/WSLPersonalities ] && ! grep -qi wsl /proc/version; then
    echo "Error: This script must be run inside a Windows Subsystem for Linux (WSL) environment."
    exit 1
fi

DISTRO="${WSL_DISTRO_NAME:-Ubuntu}"
echo "Detected WSL Distro: $DISTRO"

# Find wsl.exe path (usually in /mnt/c/Windows/System32/wsl.exe)
WSL_EXE="/mnt/c/Windows/System32/wsl.exe"
if [ ! -x "$WSL_EXE" ]; then
    # Fallback to PATH lookup
    WSL_EXE=$(which wsl.exe 2>/dev/null || true)
fi

if [ -z "$WSL_EXE" ]; then
    echo "Error: wsl.exe not found. Is Windows interop enabled in WSL?"
    exit 1
fi

echo "Using wsl.exe path: $WSL_EXE"

# 1. Wait for Moby raw socket to populate if needed
RAW_SOCKET="/mnt/wsl/docker-desktop/shared-sockets/guest-services/docker.sock"
echo "Checking Docker Desktop raw socket..."
for i in {1..10}; do
    if [ -S "$RAW_SOCKET" ] || [ -e "$RAW_SOCKET" ]; then
        break
    fi
    echo "Waiting for Docker Desktop integration socket to populate..."
    sleep 1
done

if [ ! -e "$RAW_SOCKET" ]; then
    echo "Warning: Raw socket $RAW_SOCKET not found. Docker Desktop may not be running or WSL integration is disabled."
fi

# 2. Fix /var/run/docker.sock link and permissions
echo "Fixing /var/run/docker.sock link and permissions..."
sudo rm -f /var/run/docker.sock
sudo ln -s "$RAW_SOCKET" /var/run/docker.sock
sudo chmod 666 /var/run/docker.sock

# 3. Trigger mount registration inside Moby VM by running a dummy container
echo "Triggering mount registration inside Docker..."
docker -H unix:///mnt/wsl/docker-desktop/shared-sockets/guest-services/docker.sock run --rm -v "${REPO_DIR}:/test-trigger" alpine ls /test-trigger >/dev/null 2>&1 || true

# 4. Find active Ubuntu bind mount GUID
BIND_MOUNTS_DIR="/mnt/wsl/docker-desktop-bind-mounts/${DISTRO}"
GUID=""
if [ -d "$BIND_MOUNTS_DIR" ]; then
    GUID=$(ls -1 "$BIND_MOUNTS_DIR" 2>/dev/null | head -n 1 | tr -d '\r')
fi

if [ -z "$GUID" ]; then
    GUID="f525dea9c35cdbd8225d2221946425bd125ce1dfc356a46d56148f4dc2163db1"
    echo "Using fallback WSL bind mount GUID: $GUID"
else
    echo "Resolved WSL bind mount GUID: $GUID"
fi

# 5. Manually mount the Ubuntu disk inside docker-desktop VM if not already mounted
echo "Mounting distribution virtual disk in docker-desktop VM..."
"$WSL_EXE" -d docker-desktop -u root sh -c "mkdir -p /mnt/host/wsl/docker-desktop-bind-mounts/${DISTRO}/${GUID} && mount /dev/sdd /mnt/host/wsl/docker-desktop-bind-mounts/${DISTRO}/${GUID}" >/dev/null 2>&1 || true

# 6. Find dockerd PID in VM
echo "Finding dockerd PID in docker-desktop VM..."
PID=$("$WSL_EXE" -d docker-desktop -u root pgrep dockerd | head -n 1 | tr -d '\r' || true)
if [ -z "$PID" ]; then
    echo "Error: dockerd process not found in docker-desktop VM."
    exit 1
fi
echo "dockerd PID: $PID"

# 7. Symlink repository path inside dockerd process namespace
echo "Symlinking repository path inside dockerd namespace..."
"$WSL_EXE" -d docker-desktop -u root sh -c "nsenter -t ${PID} -m -u -i -n -p rm -rf '${REPO_DIR}' && nsenter -t ${PID} -m -u -i -n -p mkdir -p '$(dirname "${REPO_DIR}")' && nsenter -t ${PID} -m -u -i -n -p ln -s '/run/desktop/mnt/host/wsl/docker-desktop-bind-mounts/${DISTRO}/${GUID}${REPO_DIR}' '${REPO_DIR}'"

echo "WSL Docker socket and mount translation successfully self-healed!"
