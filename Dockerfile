FROM python:3.12-slim

# Prevent Python from writing .pyc files and buffer stdout/stderr for real-time Docker logs
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# Create a non-root user and persistent local directories for the hospital's data & backups
RUN groupadd -r hospital && useradd -r -g hospital hospital && \
    mkdir -p /var/hospital_data /mnt/hospital_backups && \
    chown -R hospital:hospital /var/hospital_data /mnt/hospital_backups /app

# Copy requirements first to leverage Docker layer caching
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy the rest of the application source code
COPY --chown=hospital:hospital . .

# Run as non-root user for enterprise healthcare compliance
USER hospital

EXPOSE 5000

# Healthcheck so Docker automatically verifies the hospital server is responsive
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:5000/api/v1/dashboard')" || exit 1

# Use 1 worker + 8 threads so APScheduler runs a single clean backup watchdog loop
ENTRYPOINT ["gunicorn"]
CMD ["--bind=0.0.0.0:5000", "--workers=1", "--threads=8", "--timeout=60", "app:app"]
