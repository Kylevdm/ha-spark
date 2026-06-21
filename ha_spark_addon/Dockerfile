# Base set directly: Supervisor 2026.04.0+ no longer reads build.yaml / passes
# BUILD_FROM. glibc python:slim lets pip install prebuilt scikit-learn/numpy
# wheels (no musl source-compile, which broke the old Alpine image). The image
# is multi-arch (amd64 + aarch64); tzdata gives the planner real local time.
FROM python:3.13-slim-bookworm

# Supervisor still passes BUILD_VERSION = config.yaml `version`; the matching
# vX.Y.Z git tag must exist on GitHub before install/update.
ARG BUILD_VERSION
# The ML load model (load_model: ml|auto) ships here via the [habits] extra
# (scikit-learn>=1.5 + numpy>=1.26).
RUN apt-get update \
    && apt-get install -y --no-install-recommends git tzdata ca-certificates \
    && pip install --no-cache-dir \
        "ha-spark[habits] @ git+https://github.com/Kylevdm/ha-spark@v${BUILD_VERSION}" \
    && rm -rf /var/lib/apt/lists/*

# Persist the SQLite store even if the db_path option is removed.
ENV DB_PATH=/data/ha_spark.db

COPY run.sh /
RUN chmod a+x /run.sh
CMD [ "/run.sh" ]
