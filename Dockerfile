FROM python:3.12-slim
WORKDIR /app
COPY requirements.lock requirements-checkpoint.lock requirements-retrieval.lock ./
RUN pip install --no-cache-dir -r requirements.lock && pip install --no-cache-dir --no-deps --require-hashes -r requirements-checkpoint.lock
COPY logimate ./logimate
COPY scripts ./scripts
COPY tools ./tools
COPY data ./data
COPY prompts ./prompts
COPY eval ./eval
RUN mkdir -p /runtime && chown 10001:10001 /runtime
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONIOENCODING=utf-8 LOGIMATE_PROJECT_ROOT=/app
USER 10001
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=3s CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health',timeout=2)"
CMD ["python","-m","uvicorn","logimate.api:app","--host","0.0.0.0","--port","8000"]
