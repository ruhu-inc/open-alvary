# The page renderer (ingestion/render.py): pdftoppm behind a small service holding no secrets, run apart
# from the workbench (deployment/gcp-terraform/env/render.tf, deployment/azure/env/render.tf). Only the
# three modules it runs are in it. Liberation and DejaVu stand in for the Times New Roman and Arial that
# Gazette PDFs use without embedding, as in the workbench's image: without them such pages come out
# almost blank. Built from the commit by release.sh render.
FROM python:3.12-slim
RUN apt-get update && apt-get install -y --no-install-recommends poppler-utils fonts-liberation fonts-dejavu-core \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY deployment/render.requirements.lock /tmp/render.requirements.lock
RUN pip install --no-cache-dir --no-deps -r /tmp/render.requirements.lock && pip check \
    && rm /tmp/render.requirements.lock
COPY ingestion/__init__.py ingestion/tools.py ingestion/render.py ingestion/
RUN useradd --create-home --shell /usr/sbin/nologin render
USER render
EXPOSE 8080
# No worker processes (--workers 0): the service runs in the container's own process, so a start it
# refuses ends the container with an error, and the platform restarts what fails.
CMD ["hypercorn", "ingestion.render:app", "--bind", "0.0.0.0:8080", "--workers", "0"]
