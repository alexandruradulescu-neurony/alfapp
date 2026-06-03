#!/bin/bash

# LORA App - Ngrok Startup Script
# This script starts ngrok with the custom domain neurozen.ngrok.dev

echo "==================================="
echo "  LORA App - Ngrok Tunnel"
echo "  Domain: https://neurozen.ngrok.dev"
echo "==================================="
echo ""

# Check if ngrok is installed
if ! command -v ngrok &> /dev/null; then
    echo "Error: ngrok is not installed. Please install it first."
    echo "Visit: https://ngrok.com/download"
    exit 1
fi

# Check if user is authenticated with ngrok
if ! ngrok config check 2>/dev/null; then
    echo "Warning: ngrok may not be authenticated."
    echo "If you have issues, run: ngrok config add-authtoken <your_token>"
    echo ""
fi

# Kill any existing ngrok processes
pkill -f "ngrok" 2>/dev/null
sleep 1

# Start ngrok in the background with custom domain
echo "Starting ngrok tunnel..."
ngrok http --url=neurozen.ngrok.dev 8001 > /tmp/ngrok.log 2>&1 &
NGROK_PID=$!

# Wait for ngrok to initialize
sleep 4

# Check if ngrok started successfully
if ! ps -p $NGROK_PID > /dev/null; then
    echo "Error: Failed to start ngrok"
    echo "Check /tmp/ngrok.log for details"
    cat /tmp/ngrok.log
    exit 1
fi

# Show ngrok status
echo "✅ Ngrok tunnel started!"
echo "   Public URL: https://neurozen.ngrok.dev"
echo "   Local URL:  http://localhost:8001"
echo ""

# Start Django development server
echo "Starting Django server..."
echo "Press Ctrl+C to stop both servers"
echo ""

# Cleanup function
cleanup() {
    echo ""
    echo "Stopping servers..."
    kill $NGROK_PID 2>/dev/null
    pkill -f "ngrok" 2>/dev/null
    exit
}

trap cleanup INT TERM

python manage.py runserver 0.0.0.0:8001

# Cleanup when Django stops
cleanup
