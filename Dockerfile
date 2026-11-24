FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
# 틱톡 해시태그 통계용 브라우저(시스템 패키지의 Chromium을 그대로 씀)
RUN apt-get update && apt-get install -y --no-install-recommends chromium \
    && rm -rf /var/lib/apt/lists/*
ENV KT_CHROMIUM=/usr/bin/chromium
RUN pip install --no-cache-dir -r requirements.txt
COPY app app
COPY static static
COPY seed seed
# DB는 /data 에 저장 → 호스팅 서비스에서 /data 에 볼륨(영구 디스크)을 붙일 것
ENV KT_DB=/data/tracker.db
# 주소(Networking)에 지정한 포트 8000 으로 고정
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
