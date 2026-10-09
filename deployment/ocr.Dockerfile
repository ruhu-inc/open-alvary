# Local OCR for scanned gazette pages (no document leaves the machine): Poppler renders each page,
# Tesseract (English model) reads it. Operator tool only; never part of the public stack.
FROM alpine:3.21
RUN apk add --no-cache poppler-utils tesseract-ocr tesseract-ocr-data-eng \
    && adduser -D -H ocr
USER ocr
WORKDIR /work
ENTRYPOINT ["sh", "-c"]
