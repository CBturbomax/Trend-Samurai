FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app app
COPY static static
# DB는 /data 에 저장 → 호스팅 서비스에서 /data 에 볼륨(영구 디스크)을 붙일 것
ENV KT_DB=/data/tracker.db
CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000}"]
