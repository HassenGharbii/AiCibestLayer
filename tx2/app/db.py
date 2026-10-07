"""Thin persistence layer over Postgres. Plain psycopg2 + hand-written SQL —
no ORM, matching this project's minimal-dependency style for the Jetson.
"""

import os
from contextlib import contextmanager
from typing import Optional

import psycopg2
import psycopg2.extras

DATABASE_URL = os.environ.get(
    "DATABASE_URL", "postgresql://axivis:axivis@localhost:5432/axivis"
)

_pool: Optional["psycopg2.pool.SimpleConnectionPool"] = None


def _get_pool():
    global _pool
    if _pool is None:
        from psycopg2.pool import SimpleConnectionPool

        _pool = SimpleConnectionPool(1, 10, dsn=DATABASE_URL)
    return _pool


@contextmanager
def _cursor():
    pool = _get_pool()
    conn = pool.getconn()
    try:
        with conn:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                yield cur
    finally:
        pool.putconn(conn)


SCHEMA = """
CREATE TABLE IF NOT EXISTS instances (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    source_type TEXT NOT NULL,
    source_uri TEXT NOT NULL,
    config JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL
);

CREATE TABLE IF NOT EXISTS crossing_events (
    id BIGSERIAL PRIMARY KEY,
    instance_id TEXT NOT NULL REFERENCES instances(id) ON DELETE CASCADE,
    track_id INTEGER NOT NULL,
    direction TEXT NOT NULL,
    timestamp TIMESTAMPTZ NOT NULL
);
CREATE INDEX IF NOT EXISTS crossing_events_instance_idx
    ON crossing_events (instance_id, timestamp);

CREATE TABLE IF NOT EXISTS train_state_history (
    id BIGSERIAL PRIMARY KEY,
    timestamp TIMESTAMPTZ NOT NULL,
    lat DOUBLE PRECISION,
    lon DOUBLE PRECISION,
    speed_kmh DOUBLE PRECISION,
    in_station BOOLEAN,
    cab_active BOOLEAN,
    active_cab_id TEXT,
    station_label TEXT,
    destination_label TEXT,
    trip_id TEXT,
    trainset_num TEXT
);
CREATE INDEX IF NOT EXISTS train_state_history_timestamp_idx
    ON train_state_history (timestamp);
"""


def init_db() -> None:
    with _cursor() as cur:
        cur.execute(SCHEMA)


def record_instance(instance_id, name, source_type, source_uri, config, created_at) -> None:
    with _cursor() as cur:
        cur.execute(
            """
            INSERT INTO instances (id, name, source_type, source_uri, config, created_at)
            VALUES (%s, %s, %s, %s, %s, %s)
            ON CONFLICT (id) DO NOTHING
            """,
            (instance_id, name, source_type, source_uri, psycopg2.extras.Json(config), created_at),
        )


def record_crossing_event(instance_id, track_id, direction, timestamp) -> None:
    with _cursor() as cur:
        cur.execute(
            """
            INSERT INTO crossing_events (instance_id, track_id, direction, timestamp)
            VALUES (%s, %s, %s, %s)
            """,
            (instance_id, track_id, direction, timestamp),
        )


def record_train_state(state: dict) -> None:
    with _cursor() as cur:
        cur.execute(
            """
            INSERT INTO train_state_history
                (timestamp, lat, lon, speed_kmh, in_station, cab_active,
                 active_cab_id, station_label, destination_label, trip_id, trainset_num)
            VALUES
                (%(timestamp)s, %(lat)s, %(lon)s, %(speed_kmh)s, %(in_station)s, %(cab_active)s,
                 %(active_cab_id)s, %(station_label)s, %(destination_label)s, %(trip_id)s, %(trainset_num)s)
            """,
            state,
        )
