# syntax=docker/dockerfile:1.7


FROM python:3.11-slim

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
        curl \
        build-essential \
    && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir uv

COPY pyproject.toml uv.lock ./


RUN mkdir -p src/MedMAS_DDx && touch src/MedMAS_DDx/__init__.py
RUN --mount=type=cache,target=/root/.cache/uv \
    uv pip install --system -e .

COPY src/ ./src/
COPY Benchmark/PatientCases.csv ./
COPY Benchmark/MedQDx_Benchmark.csv ./
COPY graphdb/ ./graphdb/

RUN mkdir -p eval_results logs

ENTRYPOINT ["python", "-m", "MedMAS_DDx.compose_entrypoint"]

CMD ["normal"]
