CREATE TABLE IF NOT EXISTS train_state_history (
    id SERIAL PRIMARY KEY,
    train_id TEXT,
    received_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    lat DOUBLE PRECISION,
    lon DOUBLE PRECISION,
    speed_kmh DOUBLE PRECISION,
    in_station BOOLEAN,
    cab_active BOOLEAN,
    active_cab_id TEXT,
    station_label TEXT,
    destination_label TEXT,
    trip_id TEXT,
    counting_active BOOLEAN
);
CREATE INDEX IF NOT EXISTS train_state_history_received_at_idx
    ON train_state_history (received_at DESC);

CREATE TABLE IF NOT EXISTS tcms_alarms (
    id SERIAL PRIMARY KEY,
    train_id TEXT,
    alarm_source_id TEXT,
    alarm_type TEXT,
    alarm_state BOOLEAN,
    tcms_signal_name TEXT,
    vehicle_zone TEXT,
    received_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    raw JSONB
);
CREATE INDEX IF NOT EXISTS tcms_alarms_received_at_idx
    ON tcms_alarms (received_at DESC);
