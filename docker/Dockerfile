FROM python:3.11-slim

WORKDIR /app

# Install dependencies
COPY docker/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application source
COPY src/batch_job.py .

# Non-root user for security
RUN useradd -m -u 1000 batchuser
USER batchuser

ENTRYPOINT ["python", "batch_job.py"]
