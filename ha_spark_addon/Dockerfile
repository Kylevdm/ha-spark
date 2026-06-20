ARG BUILD_FROM
FROM ${BUILD_FROM}

# Supervisor passes BUILD_VERSION = config.yaml `version`; the matching
# vX.Y.Z git tag must exist on GitHub before install/update.
ARG BUILD_VERSION
# NB: the optional ML load model (load_model: ml|auto) is NOT installed here.
# scikit-learn ships no musllinux wheels (neither PyPI nor the HA wheel index),
# so on Alpine/musl pip falls back to a source compile and drags in a C/Fortran
# toolchain + OpenBLAS. ml.py is import-guarded; forecast.py degrades to the
# slot-profile median. Re-add "scikit-learn"/"numpy" if a musl wheel ever ships.
RUN apk add --no-cache git \
    && pip install --no-cache-dir "git+https://github.com/Kylevdm/ha-spark@v${BUILD_VERSION}" \
    && apk del git

# Persist the SQLite store even if the db_path option is removed.
ENV DB_PATH=/data/ha_spark.db

COPY run.sh /
RUN chmod a+x /run.sh
CMD [ "/run.sh" ]
