FROM python:3.12-slim
WORKDIR /app
COPY . .
RUN pip install --no-cache-dir -r requirements.lock && pip install --no-deps .
RUN useradd --create-home corpus && chown -R corpus:corpus /app
USER corpus
EXPOSE 8018
CMD ["uvicorn", "api.main:app", "--host", "0.0.0.0", "--port", "8018"]
