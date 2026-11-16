FROM python:3.12-slim@sha256:5e2bdb1b0d542d5039cffb8181c65930093949194a65c365dfb1b0fe13a4fa5b
WORKDIR /app
COPY . .
RUN pip install --no-cache-dir -r requirements.lock && pip install --no-deps .
RUN useradd --create-home corpus && chown -R corpus:corpus /app
USER corpus
EXPOSE 8018
CMD ["uvicorn", "api.service:app", "--host", "0.0.0.0", "--port", "8018", "--no-access-log"]
