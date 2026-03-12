#!/bin/bash
# LORA production deployment script
# Called by GitHub Actions on push to main
set -euo pipefail

APP_DIR="/opt/lora"
VENV="$APP_DIR/venv"
LOG_FILE="/var/log/lora/deploy.log"

echo "=== Deploy started at $(date -Iseconds) ===" | tee -a "$LOG_FILE"

cd "$APP_DIR"

# Pull latest code
echo "Pulling latest changes..."
git fetch origin main
git reset --hard origin/main

# Activate virtualenv and install deps
echo "Installing dependencies..."
source "$VENV/bin/activate"
pip install -r requirements.txt --quiet

# Run migrations
echo "Running migrations..."
python manage.py migrate --noinput

# Collect static files
echo "Collecting static files..."
python manage.py collectstatic --noinput --clear

# Restart application
echo "Restarting services..."
sudo systemctl restart lora

# Verify it came back up
sleep 2
if systemctl is-active --quiet lora; then
    echo "Deploy successful — service is running" | tee -a "$LOG_FILE"
else
    echo "ERROR: Service failed to start!" | tee -a "$LOG_FILE"
    sudo journalctl -u lora --no-pager -n 20 | tee -a "$LOG_FILE"
    exit 1
fi

echo "=== Deploy finished at $(date -Iseconds) ===" | tee -a "$LOG_FILE"
