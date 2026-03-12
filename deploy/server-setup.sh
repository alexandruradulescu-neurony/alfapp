#!/bin/bash
# LORA — One-time server setup for Hetzner CX22 (Ubuntu 22.04)
# Run as root on a fresh server
#
# Usage: sudo bash server-setup.sh YOUR_DOMAIN YOUR_EMAIL
#
# After running this script:
# 1. Add GitHub deploy key: ssh-keygen -t ed25519 on server, add public key to GitHub repo
# 2. Clone repo: su - deploy -c "git clone git@github.com:alexandruradulescu-neurony/alfapp.git /opt/lora"
# 3. Create .env: cp /opt/lora/.env.example /opt/lora/.env && nano /opt/lora/.env
# 4. Set DATABASE_URL=postgres://lora:YOUR_DB_PASSWORD@localhost:5432/lora
# 5. First deploy: su - deploy -c "bash /opt/lora/deploy/deploy.sh"
# 6. Add GitHub Actions secrets: SSH_HOST, SSH_USER (deploy), SSH_KEY

set -euo pipefail

DOMAIN="${1:?Usage: $0 DOMAIN EMAIL}"
EMAIL="${2:?Usage: $0 DOMAIN EMAIL}"
DEPLOY_USER="deploy"
APP_DIR="/opt/lora"

echo "=== LORA Server Setup ==="
echo "Domain: $DOMAIN"
echo "Email: $EMAIL"

# ── System packages ──────────────────────────────────────────────
apt-get update && apt-get upgrade -y
apt-get install -y \
    python3.10 python3.10-venv python3.10-dev python3-pip \
    postgresql postgresql-contrib \
    nginx certbot python3-certbot-nginx \
    libpango-1.0-0 libpangocairo-1.0-0 libgdk-pixbuf2.0-0 \
    libcairo2 libffi-dev \
    git curl ufw

# ── Firewall ─────────────────────────────────────────────────────
ufw allow OpenSSH
ufw allow "Nginx Full"
ufw --force enable

# ── Deploy user ──────────────────────────────────────────────────
if ! id "$DEPLOY_USER" &>/dev/null; then
    useradd -m -s /bin/bash "$DEPLOY_USER"
    echo "$DEPLOY_USER ALL=(ALL) NOPASSWD: /bin/systemctl restart lora, /bin/systemctl status lora" \
        > /etc/sudoers.d/lora
fi

# ── App directory ────────────────────────────────────────────────
mkdir -p "$APP_DIR" /var/log/lora /run/lora
chown "$DEPLOY_USER:$DEPLOY_USER" "$APP_DIR" /var/log/lora /run/lora

# ── Python virtualenv ────────────────────────────────────────────
su - "$DEPLOY_USER" -c "python3.10 -m venv $APP_DIR/venv"

# ── PostgreSQL ───────────────────────────────────────────────────
DB_PASS=$(openssl rand -base64 24)
sudo -u postgres psql -c "CREATE USER lora WITH PASSWORD '$DB_PASS';" 2>/dev/null || true
sudo -u postgres psql -c "CREATE DATABASE lora OWNER lora;" 2>/dev/null || true
sudo -u postgres psql -c "ALTER USER lora CREATEDB;"  # for running tests

echo ""
echo "─────────────────────────────────────────"
echo "Database password (save this): $DB_PASS"
echo "DATABASE_URL=postgres://lora:${DB_PASS}@localhost:5432/lora"
echo "─────────────────────────────────────────"
echo ""

# ── Playwright browsers ──────────────────────────────────────────
su - "$DEPLOY_USER" -c "
    source $APP_DIR/venv/bin/activate
    pip install playwright
    playwright install chromium --with-deps
"

# ── Nginx ────────────────────────────────────────────────────────
# Initial HTTP-only config for Certbot validation
cat > /etc/nginx/sites-available/lora <<NGINX
server {
    listen 80;
    server_name $DOMAIN;

    location /.well-known/acme-challenge/ {
        root /var/www/certbot;
    }

    location / {
        return 301 https://\$server_name\$request_uri;
    }
}
NGINX

ln -sf /etc/nginx/sites-available/lora /etc/nginx/sites-enabled/lora
rm -f /etc/nginx/sites-enabled/default
mkdir -p /var/www/certbot
nginx -t && systemctl reload nginx

# ── SSL certificate ──────────────────────────────────────────────
certbot --nginx -d "$DOMAIN" --email "$EMAIL" --agree-tos --non-interactive --redirect

# ── Replace with full Nginx config ───────────────────────────────
echo "After cloning the repo, copy the full Nginx config:"
echo "  cp $APP_DIR/deploy/nginx.conf /etc/nginx/sites-available/lora"
echo "  sed -i 's/YOUR_DOMAIN/$DOMAIN/g' /etc/nginx/sites-available/lora"
echo "  nginx -t && systemctl reload nginx"

# ── Systemd service ──────────────────────────────────────────────
echo "After cloning the repo, enable the service:"
echo "  cp $APP_DIR/deploy/lora.service /etc/systemd/system/lora.service"
echo "  systemctl daemon-reload"
echo "  systemctl enable lora"

# ── Logrotate ────────────────────────────────────────────────────
cat > /etc/logrotate.d/lora <<LOGROTATE
/var/log/lora/*.log {
    weekly
    rotate 4
    compress
    delaycompress
    missingok
    notifempty
    postrotate
        systemctl reload lora 2>/dev/null || true
    endscript
}
LOGROTATE

echo ""
echo "=== Setup complete ==="
echo ""
echo "Next steps:"
echo "1. Generate SSH key:  su - deploy -c 'ssh-keygen -t ed25519'"
echo "2. Add public key to GitHub repo as deploy key"
echo "3. Clone repo:        su - deploy -c 'git clone git@github.com:alexandruradulescu-neurony/alfapp.git $APP_DIR'"
echo "4. Create .env:       cp $APP_DIR/.env.example $APP_DIR/.env"
echo "5. Edit .env — set DATABASE_URL, SECRET_KEY, ENCRYPTION_KEY, DEBUG=False, ALLOWED_HOSTS=$DOMAIN"
echo "6. Copy configs:      cp $APP_DIR/deploy/lora.service /etc/systemd/system/"
echo "                      cp $APP_DIR/deploy/nginx.conf /etc/nginx/sites-available/lora"
echo "                      sed -i 's/YOUR_DOMAIN/$DOMAIN/g' /etc/nginx/sites-available/lora"
echo "7. Enable service:    systemctl daemon-reload && systemctl enable lora"
echo "8. First deploy:      su - deploy -c 'bash $APP_DIR/deploy/deploy.sh'"
echo "9. Reload Nginx:      nginx -t && systemctl reload nginx"
echo "10. Add GitHub secrets: SSH_HOST=$(curl -s ifconfig.me), SSH_USER=deploy, SSH_KEY"
