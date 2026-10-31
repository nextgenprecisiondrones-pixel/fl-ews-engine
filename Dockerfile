FROM python:3.12-slim
WORKDIR /app
COPY pyproject.toml README.md ./
COPY ews ./ews
RUN pip install --no-cache-dir . && python -m ews.synth --out /app/data --seed 42
ENV EWS_DATA_DIR=/app/data
EXPOSE 8000
CMD ["uvicorn", "ews.api:app", "--host", "0.0.0.0", "--port", "8000"]
