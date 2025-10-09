# Proxmox LXC Auto Start/Stop System - Dockerfile
# Base image with Python 3.11 on Alpine for smaller size and security
FROM python:3.11-slim

# Set working directory
WORKDIR /app

# Install system dependencies required for the application
RUN apt-get update && apt-get install -y \
    curl \
    ca-certificates \
    tzdata \
    && rm -rf /var/lib/apt/lists/* \
    && apt-get clean

# Create non-root user for security
RUN groupadd -r appuser && useradd -r -g appuser -d /app -s /bin/sh appuser

# Copy requirements first for better Docker layer caching
COPY requirements.txt .

# Install Python dependencies
RUN pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY asgi_app.py .

# Create necessary directories and set permissions
RUN mkdir -p /app/logs /app/data \
    && chown -R appuser:appuser /app

# Switch to non-root user
USER appuser

# Expose the application port
EXPOSE 8080

# Health check to ensure the service is running
HEALTHCHECK --interval=30s --timeout=5s --start-period=40s --retries=3 \
    CMD curl -f http://localhost:8080/healthz || exit 1

# Run the application using uvicorn
CMD ["uvicorn", "asgi_app:app", "--host", "0.0.0.0", "--port", "8080", "--log-level", "info"]