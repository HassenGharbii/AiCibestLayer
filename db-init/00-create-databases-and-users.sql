-- Runs once, against the default POSTGRES_DB, as the superuser.
-- Creates one database per service, each with its own matching user,
-- so tx2 and mqtt-service keep using the exact connection strings they
-- already default to (no app code changes needed).

CREATE USER axivis WITH PASSWORD 'axivis';
CREATE DATABASE axivis OWNER axivis;

CREATE USER mqtt_bridge WITH PASSWORD 'mqtt_bridge';
CREATE DATABASE mqtt_bridge OWNER mqtt_bridge;
