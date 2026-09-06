# Local tests only. Never use this image as a production publication helper.
ARG HELPER_BASE=ghcr.io/preloop/preloop@sha256:07aea6618eb032c4e4fd7cea3334761dfabf93282f65e1fd7668d1d851850b07
FROM ${HELPER_BASE}
COPY backend/preloop /app/backend/preloop
COPY scripts/tests/publication_helper_fixture/sitecustomize.py /fixture/sitecustomize.py
ENV PYTHONPATH=/fixture:/app/backend PRELOOP_DISABLE_TELEMETRY=true
ENTRYPOINT ["python"]
