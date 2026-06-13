ARG BUILD_FROM
FROM ${BUILD_FROM}

# Supervisor passes BUILD_VERSION = config.yaml `version`; the matching
# vX.Y.Z git tag must exist on GitHub before install/update.
ARG BUILD_VERSION
# scikit-learn/numpy power the optional ML load model (load_model: ml|auto).
# Installed explicitly rather than via the [habits] extra: river (Phase 6E)
# ships no musllinux wheels and would drag a compiler into the image.
RUN apk add --no-cache git \
    && pip install --no-cache-dir "git+https://github.com/Kylevdm/ha-spark@v${BUILD_VERSION}" \
        "scikit-learn>=1.5" "numpy>=1.26" \
    && apk del git

# Persist the SQLite store even if the db_path option is removed.
ENV DB_PATH=/data/ha_spark.db

COPY run.sh /
RUN chmod a+x /run.sh
CMD [ "/run.sh" ]
