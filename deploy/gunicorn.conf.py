# Gunicorn configuration for LORA
# Sized for Hetzner CX22 (2 vCPU, 4GB RAM)

import multiprocessing

# Server socket
bind = "unix:/run/lora/gunicorn.sock"

# Workers: 2 × CPU + 1
workers = multiprocessing.cpu_count() * 2 + 1

# Timeout — 120s to allow PDF generation and Playwright screenshots
timeout = 120
graceful_timeout = 30

# Logging
accesslog = "/var/log/lora/gunicorn-access.log"
errorlog = "/var/log/lora/gunicorn-error.log"
loglevel = "info"

# Process naming
proc_name = "lora"

# Security
limit_request_line = 8190
limit_request_fields = 100

# Restart workers after this many requests (prevents memory leaks from Playwright/WeasyPrint)
max_requests = 500
max_requests_jitter = 50
