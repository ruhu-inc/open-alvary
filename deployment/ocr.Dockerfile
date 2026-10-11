# Local OCR for scanned gazette pages (no document leaves the machine): Poppler renders each page,
# Tesseract (English model) reads it. Operator tool only; never part of the public stack.
# Fonts: many gazettes (Rwanda's among them) do not embed Times New Roman or Arial; without
# metric-compatible substitutes Poppler renders their pages almost blank.
FROM alpine:3.21
RUN apk add --no-cache poppler-utils tesseract-ocr tesseract-ocr-data-eng font-liberation font-dejavu \
    && adduser -D -H ocr
USER ocr
WORKDIR /work
ENTRYPOINT ["sh", "-c"]
