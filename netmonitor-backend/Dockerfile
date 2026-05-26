FROM python:3.11-slim

# Set working directory
WORKDIR /app

# Install dependencies first (layer caching — only rebuilds when requirements change)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY . .

# Expose the application port
EXPOSE 8000

# Run uvicorn — PORT env var is honoured by PaaS providers (Railway, Render, Fly.io)
CMD ["sh", "-c", "uvicorn main:app --host 0.0.0.0 --port ${PORT:-8000}"]
