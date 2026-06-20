#!/usr/bin/env bash
# Startup: log a health report (never fatal), then hand over to the daemon.
# Plain shell — the glibc python:slim base has no bashio/s6 (Supervisor runs
# tini as PID 1 via `init: true`, so signals reach the daemon directly).
set +e

echo "[ha-spark] starting; running health check..."
ha-spark health
rc=$?
if [ "${rc}" -ne 0 ]; then
    echo "[ha-spark] health check reported issues (exit ${rc}); continuing"
fi

echo "[ha-spark] starting run daemon"
exec ha-spark run
