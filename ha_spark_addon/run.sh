#!/usr/bin/with-contenv bashio
# Startup: log a health report (never fatal), then hand over to the daemon.
set +e

bashio::log.info "ha-spark starting; running health check..."
ha-spark health
rc=$?
if [ "${rc}" -ne 0 ]; then
    bashio::log.warning "health check reported issues (exit ${rc}); continuing"
fi

bashio::log.info "Starting ha-spark run daemon"
exec ha-spark run
