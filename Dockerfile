FROM python:3.11-slim

WORKDIR /app

COPY pyproject.toml .
RUN pip install --no-cache-dir -e . 2>/dev/null || true

COPY . .
RUN pip install --no-cache-dir -e .

EXPOSE 10000

CMD ["sh", "start.sh"]
