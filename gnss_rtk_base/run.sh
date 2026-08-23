#!/usr/bin/with-contenv bashio
set -e

if bashio::services.available "mqtt"; then
    export MQTT_HOST=$(bashio::services mqtt "host")
    export MQTT_PORT=$(bashio::services mqtt "port")
    export MQTT_USER=$(bashio::services mqtt "username")
    export MQTT_PASSWORD=$(bashio::services mqtt "password")
    bashio::log.info "MQTT broker: ${MQTT_HOST}:${MQTT_PORT}"
else
    bashio::log.warning "No MQTT broker available. Install/configure the Mosquitto broker add-on (or the MQTT integration) before using this add-on."
fi

bashio::log.info "Starting RTK Base Station add-on..."
exec python3 /app/main.py
