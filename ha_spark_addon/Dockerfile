ARG BUILD_FROM
FROM ${BUILD_FROM}

# Supervisor passes BUILD_VERSION = config.yaml `version`; the matching
# vX.Y.Z git tag must exist on GitHub before install/update.
ARG BUILD_VERSION
# The ML load model (load_model: ml|auto) ships here. We build on a glibc base
# (python:slim, set in build.yaml) so pip installs prebuilt scikit-learn/numpy
# wheels — no musl source-compile, which is what broke the Alpine image. The
# [habits] extra pulls scikit-learn>=1.5 + numpy>=1.26; tzdata gives the planner
# real local time instead of the UTC fallback.
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
