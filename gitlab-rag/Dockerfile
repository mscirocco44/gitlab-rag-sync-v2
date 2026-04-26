FROM python:3.11-slim
WORKDIR /app
RUN pip install --no-cache-dir requests
COPY sync.py .

# Run as non-root for STIG/security compliance
RUN useradd --create-home --shell /usr/sbin/nologin --uid 10001 syncuser \
 && chown -R syncuser:syncuser /app
USER syncuser

CMD ["python", "-u", "sync.py"]
