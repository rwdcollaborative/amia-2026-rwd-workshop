#!/usr/bin/env bash
# One-time setup for a fresh Ubuntu 24.04 droplet. Run as root:
#   bash setup-droplet.sh
# Safe to rerun. Installs Docker, adds swap, rotates container logs, and
# makes SSH key-only. Firewalling is done by the DigitalOcean Cloud Firewall
# (see README): Docker's published ports bypass ufw, so ufw is not used.
set -euo pipefail

[[ $EUID -eq 0 ]] || { echo "run as root" >&2; exit 1; }
. /etc/os-release
[[ "$ID" == ubuntu ]] || { echo "expected Ubuntu, got $ID" >&2; exit 1; }

echo "==> System updates"
export DEBIAN_FRONTEND=noninteractive
apt-get update -q
apt-get upgrade -yq
apt-get install -yq ca-certificates curl unzip unattended-upgrades
# Ubuntu enables these by default; make sure security updates install daily.
cat > /etc/apt/apt.conf.d/20auto-upgrades <<'EOF'
APT::Periodic::Update-Package-Lists "1";
APT::Periodic::Unattended-Upgrade "1";
EOF

echo "==> Docker"
if ! command -v docker >/dev/null; then
  install -m 0755 -d /etc/apt/keyrings
  curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
  chmod a+r /etc/apt/keyrings/docker.asc
  echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/ubuntu ${VERSION_CODENAME} stable" \
    > /etc/apt/sources.list.d/docker.list
  apt-get update -q
  apt-get install -yq docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
fi
# Cap container logs so a chatty WebAPI can't fill the disk.
if [[ ! -f /etc/docker/daemon.json ]]; then
  cat > /etc/docker/daemon.json <<'EOF'
{ "log-driver": "local", "log-opts": { "max-size": "20m", "max-file": "3" } }
EOF
  systemctl restart docker
fi
systemctl enable --now docker

echo "==> Swap"
# Headroom for the WebAPI Maven build and Solr import on an 8 GB droplet.
if ! swapon --show | grep -q /swapfile; then
  fallocate -l 4G /swapfile
  chmod 600 /swapfile
  mkswap /swapfile
  swapon /swapfile
  grep -q '^/swapfile' /etc/fstab || echo '/swapfile none swap sw 0 0' >> /etc/fstab
fi

echo "==> SSH: keys only"
# Refuse to lock ourselves out.
[[ -s /root/.ssh/authorized_keys ]] || { echo "no SSH key in /root/.ssh/authorized_keys; not touching sshd" >&2; exit 1; }
# sshd uses the first value it sees, and cloud-init's 50-cloud-init.conf can
# turn passwords back on, so this must sort before it.
cat > /etc/ssh/sshd_config.d/00-hardening.conf <<'EOF'
PasswordAuthentication no
KbdInteractiveAuthentication no
PermitRootLogin prohibit-password
EOF
sshd -t
systemctl reload ssh

echo "Done. Docker $(docker --version | cut -d' ' -f3 | tr -d ,), compose $(docker compose version --short)."
