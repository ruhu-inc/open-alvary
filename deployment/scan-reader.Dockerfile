# The scan-reading job (ingestion/scan_job.py): reads the corpus's scanned pages with Document AI,
# from the corpus bucket and back to it. Built by deployment/gcp/scan_reading.sh from committed code;
# .gcloudignore keeps private material (private-staging, docs, .env) out of the build.
FROM python:3.12-slim
WORKDIR /app
COPY . .
RUN pip install --no-cache-dir -r requirements.lock -r deployment/scan-reader.requirements.lock \
    && pip install --no-cache-dir --no-deps .
RUN useradd --create-home reader && chown -R reader:reader /app
USER reader
ENTRYPOINT ["python", "-m", "ingestion.scan_job"]
