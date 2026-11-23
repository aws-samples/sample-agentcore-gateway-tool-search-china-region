# Build only after scripts/build_lambda.py has produced the hash-locked x86_64 ZIP.
# Preserve the Lambda Python base family; pin the official Linux amd64 manifest.
FROM public.ecr.aws/lambda/python:3.12@sha256:8366d766893e53d1d7ba4f9d2fd9adff47f68093a762c0bba3c54035e6ffb569
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app \
    HOME=/tmp \
    SEMSEARCH_ECS_PORT=8443 \
    SEMSEARCH_ECS_WORKERS=4 \
    SEMSEARCH_ECS_HEALTH_PORT=9443
WORKDIR /app
COPY .artifacts/lambda.zip /tmp/search.zip
RUN python -m zipfile -e /tmp/search.zip /app \
    && rm /tmp/search.zip \
    && chmod -R a=rX /app
USER 10001:10001
EXPOSE 8443 9443
HEALTHCHECK --interval=30s --timeout=5s --start-period=120s --retries=3 \
    CMD ["python", "-m", "semantic_service.ecs_health"]
STOPSIGNAL SIGTERM
ENTRYPOINT ["python", "-m", "semantic_service.ecs_server"]
