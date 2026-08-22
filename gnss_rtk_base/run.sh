#!/usr/bin/with-contenv bashio
set -e

if bashio::services.available "mqtt"; then
    export MQTT_HOST=$(bashio::services mqtt "host")
    export MQTT_PORT=$(bashio::services mqtt "port")
    export MQTT_USER=$(bashio::services mqtt "username")
    export MQTT_PASSWORD=$(bashio::services mqtt "password")
    bashio::log.info "Broker MQTT: ${MQTT_HOST}:${MQTT_PORT}"
else
    bashio::log.warning "Nessun broker MQTT disponibile. Installa/configura l'add-on Mosquitto broker (o l'integrazione MQTT) prima di usare questo add-on."
fi

bashio::log.info "Avvio UM982 RTK add-on..."
exec python3 /app/main.py
