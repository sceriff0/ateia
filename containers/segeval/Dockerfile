FROM python:3.11-slim

LABEL description="Mirage SEG_QUALITY_EVAL: vendored CellSegmentationEvaluator (2D) metrics"

RUN apt-get update && apt-get install -y --no-install-recommends \
        libgl1 libglib2.0-0 procps \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir -r /tmp/requirements.txt \
    && pip install --no-cache-dir "numpy<2.0"

CMD ["python3"]
