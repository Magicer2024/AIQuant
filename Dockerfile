FROM python:3.11-slim

WORKDIR /app

# Build dependencies for numpy/pandas
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    && rm -rf /var/lib/apt/lists/*

# Install Python deps（constraints.txt 锁定可复现版本；Qlib 为可选依赖，
# 基础镜像不强装 —— 需要时另装 requirements-qlib.txt 并设 AIQUANT_QLIB_ENABLED=1）
COPY requirements.txt constraints.txt ./
RUN pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir -r requirements.txt -c constraints.txt

# Copy app source
COPY . .

EXPOSE 5000

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:5000/api/health').read()" || exit 1

CMD ["python", "app.py"]
