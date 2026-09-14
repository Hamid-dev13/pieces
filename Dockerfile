# Image de pieces. Volontairement nue : ni poppler ni tesseract tant que
# l'extraction n'est pas écrite — ils arriveront avec les stories ext-1 et
# ext-2, pas avant.
FROM python:3.13-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY src/ ./src/

CMD ["python", "-m", "src.main"]
