# Generic fixture site for browser QA flows. Synthetic credentials only.
# Build from this directory:
#   docker build -t fixture-site environments/fixture-site
# python:3.12-slim
FROM python:3.12-slim@sha256:5e2bdb1b0d542d5039cffb8181c65930093949194a65c365dfb1b0fe13a4fa5b
WORKDIR /app
COPY server.py ./
USER nobody
EXPOSE 8080
ENV FIXTURE_PORT=8080 PYTHONUNBUFFERED=1
ENTRYPOINT ["python3", "/app/server.py"]
