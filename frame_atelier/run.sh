#!/usr/bin/with-contenv bashio
# shellcheck shell=bash
bashio::log.info "Starting Frame Atelier…"
exec python3 /app/app.py
