# The review workbench on Cloud Run (deployment/gcp/workbench.sh). Page images are drawn on demand
# by pdftoppm in this container, which is the sandbox (ALVARY_RENDER=direct); Liberation and DejaVu
# stand in for the Times New Roman and Arial that Gazette PDFs use without embedding. Source PDFs,
# readings and images fetched from the corpus bucket are cached in /tmp, kept within
# ALVARY_REVIEW_CACHE_MB (Cloud Run's /tmp is the instance's memory).
FROM python:3.12-slim
RUN apt-get update && apt-get install -y --no-install-recommends poppler-utils fonts-liberation fonts-dejavu-core \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY . .
RUN pip install --no-cache-dir -r requirements.lock -r deployment/ingestion-jobs.requirements.lock \
    && pip install --no-cache-dir --no-deps .
RUN useradd --create-home workbench && chown -R workbench:workbench /app
USER workbench
ENV ALVARY_RENDER=direct ALVARY_REVIEW_CACHE=/tmp/review-cache ALVARY_REVIEW_CACHE_MB=600
EXPOSE 8080
CMD ["uvicorn", "workbench.app:app", "--host", "0.0.0.0", "--port", "8080", "--proxy-headers", "--forwarded-allow-ips", "*", "--no-access-log"]
