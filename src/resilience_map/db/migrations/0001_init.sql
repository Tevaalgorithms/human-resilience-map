-- Human Resilience Map: initial schema.
-- Schemas: hrm (application), checkpoints (LangGraph), sessions (Dash server-side sessions).
CREATE EXTENSION IF NOT EXISTS postgis;
CREATE EXTENSION IF NOT EXISTS pgcrypto;
CREATE SCHEMA IF NOT EXISTS hrm;
CREATE SCHEMA IF NOT EXISTS checkpoints;
CREATE SCHEMA IF NOT EXISTS sessions;

SET search_path = hrm, public;

CREATE TABLE study_areas (
    id          text PRIMARY KEY,
    name        text NOT NULL,
    description text NOT NULL DEFAULT '',
    is_synthetic boolean NOT NULL DEFAULT false
);

CREATE TABLE dataset_versions (
    id               uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    study_area_id    text NOT NULL REFERENCES study_areas (id),
    version          text NOT NULL,
    status           text NOT NULL CHECK (status IN ('building', 'published', 'failed', 'retired')),
    scoring_version  text NOT NULL,
    source_manifest  jsonb NOT NULL DEFAULT '[]',   -- URLs, licenses, checksums, observation periods
    validation_report jsonb NOT NULL DEFAULT '{}',
    created_at       timestamptz NOT NULL DEFAULT now(),
    published_at     timestamptz,
    UNIQUE (study_area_id, version)
);
CREATE INDEX dataset_versions_published_idx
    ON dataset_versions (study_area_id, published_at DESC) WHERE status = 'published';

CREATE TABLE tracts (
    dataset_version_id uuid NOT NULL REFERENCES dataset_versions (id) ON DELETE CASCADE,
    tract_id           text NOT NULL CHECK (tract_id ~ '^[0-9A-Za-z-]{3,20}$'),
    name               text NOT NULL,
    population         integer NOT NULL CHECK (population >= 0),
    geom               geometry(MultiPolygon, 4326) NOT NULL,
    centroid           geometry(Point, 4326) NOT NULL,
    PRIMARY KEY (dataset_version_id, tract_id)
);
CREATE INDEX tracts_geom_idx ON tracts USING gist (geom);

CREATE TABLE tract_indicators (
    dataset_version_id uuid NOT NULL,
    tract_id           text NOT NULL,
    surface_temp_anomaly_c  double precision,   -- land-surface temperature anomaly (NOT air temp)
    pct_age_65_plus         double precision CHECK (pct_age_65_plus BETWEEN 0 AND 100),
    median_income_usd       double precision CHECK (median_income_usd >= 0),
    median_income_moe_usd   double precision CHECK (median_income_moe_usd >= 0),
    canopy_pct              double precision CHECK (canopy_pct BETWEEN 0 AND 100),
    transit_access_index    double precision CHECK (transit_access_index BETWEEN 0 AND 1),
    source_dates            jsonb NOT NULL DEFAULT '{}',
    PRIMARY KEY (dataset_version_id, tract_id),
    FOREIGN KEY (dataset_version_id, tract_id)
        REFERENCES tracts (dataset_version_id, tract_id) ON DELETE CASCADE
);

CREATE TABLE cooling_centers (
    dataset_version_id uuid NOT NULL REFERENCES dataset_versions (id) ON DELETE CASCADE,
    center_id          text NOT NULL,
    name               text NOT NULL,
    capacity           integer NOT NULL CHECK (capacity >= 0),
    hours              text NOT NULL DEFAULT '',
    last_confirmed     date,
    geom               geometry(Point, 4326) NOT NULL,
    PRIMARY KEY (dataset_version_id, center_id)
);
CREATE INDEX cooling_centers_geom_idx ON cooling_centers USING gist (geom);

CREATE TABLE candidate_sites (
    dataset_version_id uuid NOT NULL REFERENCES dataset_versions (id) ON DELETE CASCADE,
    site_id            text NOT NULL,
    name               text NOT NULL,
    capacity           integer NOT NULL CHECK (capacity > 0),
    cost_usd           double precision NOT NULL CHECK (cost_usd >= 0),
    accessible         boolean NOT NULL DEFAULT true,
    last_confirmed     date,
    notes              text NOT NULL DEFAULT '',
    geom               geometry(Point, 4326) NOT NULL,
    PRIMARY KEY (dataset_version_id, site_id)
);
CREATE INDEX candidate_sites_geom_idx ON candidate_sites USING gist (geom);

CREATE TABLE tract_scores (
    dataset_version_id uuid NOT NULL,
    tract_id           text NOT NULL,
    scoring_version    text NOT NULL,
    score              double precision CHECK (score BETWEEN 0 AND 100),  -- NULL = insufficient data
    components         jsonb NOT NULL,
    missing_indicators jsonb NOT NULL DEFAULT '[]',
    nearest_center_km  double precision,
    PRIMARY KEY (dataset_version_id, tract_id, scoring_version),
    FOREIGN KEY (dataset_version_id, tract_id)
        REFERENCES tracts (dataset_version_id, tract_id) ON DELETE CASCADE
);

CREATE TABLE jobs (
    id                 uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    owner_id           text NOT NULL,
    idempotency_key    text,
    status             text NOT NULL DEFAULT 'queued'
        CHECK (status IN ('queued', 'running', 'needs_input', 'completed', 'incomplete', 'failed')),
    question           text NOT NULL,
    request            jsonb NOT NULL,
    study_area_id      text NOT NULL REFERENCES study_areas (id),
    dataset_version_id uuid NOT NULL REFERENCES dataset_versions (id),
    scoring_version    text NOT NULL,
    progress           text NOT NULL DEFAULT 'queued',
    clarification      jsonb,                         -- populated while needs_input
    attempts           integer NOT NULL DEFAULT 0,
    claimed_by         text,
    lease_expires_at   timestamptz,
    error              text,
    created_at         timestamptz NOT NULL DEFAULT now(),
    updated_at         timestamptz NOT NULL DEFAULT now(),
    completed_at       timestamptz
);
CREATE UNIQUE INDEX jobs_idempotency_idx ON jobs (owner_id, idempotency_key)
    WHERE idempotency_key IS NOT NULL;
CREATE INDEX jobs_owner_idx ON jobs (owner_id, created_at DESC);

CREATE TABLE outbox (
    id            bigserial PRIMARY KEY,
    job_id        uuid NOT NULL REFERENCES jobs (id) ON DELETE CASCADE,
    kind          text NOT NULL DEFAULT 'run',       -- run | resume
    payload       jsonb NOT NULL DEFAULT '{}',
    created_at    timestamptz NOT NULL DEFAULT now(),
    dispatched_at timestamptz
);
CREATE INDEX outbox_pending_idx ON outbox (id) WHERE dispatched_at IS NULL;

CREATE TABLE job_results (
    job_id     uuid PRIMARY KEY REFERENCES jobs (id) ON DELETE CASCADE,  -- one result per job
    outcome    text NOT NULL CHECK (outcome IN ('completed', 'incomplete')),
    result     jsonb NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE human_reviews (
    id         bigserial PRIMARY KEY,
    job_id     uuid NOT NULL REFERENCES jobs (id) ON DELETE CASCADE,
    site_id    text NOT NULL,
    reviewer   text NOT NULL,
    decision   text NOT NULL CHECK (decision IN ('approved', 'rejected', 'needs_followup')),
    notes      text NOT NULL DEFAULT '',
    created_at timestamptz NOT NULL DEFAULT now()
);

-- Local-development queue adapter (SQS is used on AWS).
CREATE TABLE queue_messages (
    id            bigserial PRIMARY KEY,
    body          jsonb NOT NULL,
    receive_count integer NOT NULL DEFAULT 0,
    visible_at    timestamptz NOT NULL DEFAULT now(),
    dead          boolean NOT NULL DEFAULT false
);

CREATE TABLE sessions.sessions (
    id         text PRIMARY KEY,
    data       jsonb NOT NULL,
    expires_at timestamptz NOT NULL
);
CREATE INDEX sessions_expiry_idx ON sessions.sessions (expires_at);
