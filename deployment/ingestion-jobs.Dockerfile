# The ingestion jobs on Cloud Run (deployment/gcp/ingestion_jobs.sh), one image:
#   scan-reader      python -m ingestion.scan_job     reads scanned pages with Document AI
#   corpus-preparer  python -m ingestion.prepare_job  prepares every file for review
# Built from the commit itself (git archive); .gcloudignore keeps private material out regardless.
# pdftotext reads each PDF's text layer here: the job's container is its sandbox.
FROM python:3.12-slim
RUN apt-get update && apt-get install -y --no-install-recommends poppler-utils \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY . .
RUN pip install --no-cache-dir -r requirements.lock -r deployment/ingestion-jobs.requirements.lock \
    && pip install --no-cache-dir --no-deps .
RUN useradd --create-home jobs && chown -R jobs:jobs /app
USER jobs
ENV ALVARY_TEXT_LAYER=direct
ENTRYPOINT ["python", "-m"]
CMD ["ingestion.scan_job"]
