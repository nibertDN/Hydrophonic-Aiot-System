-- Smart AI Irrigation - schema. Safe to run more than once (idempotent).
-- Run in Supabase -> SQL Editor. Does NOT drop or modify existing data or existing policies.
--
-- Columns the backend reads/writes in public.sensor_readings:
--   device_id, soil_value, soil_dry, water_low, water_red_led, water_green_led,
--   soil_red_led, soil_green_led, buzzer_on, is_simulated, rssi, uptime_s, created_at
-- Missing ones are ADDED. If your table already uses different names for the same data
-- (e.g. "soil_moisture"), see README "Using your existing table".

create table if not exists public.sensor_readings (
  id bigint generated always as identity primary key
);

alter table public.sensor_readings
  add column if not exists device_id        text        not null default 'device01',
  add column if not exists soil_value       integer,
  add column if not exists soil_dry         boolean     not null default false,
  add column if not exists water_low        boolean     not null default false,
  add column if not exists water_red_led    boolean     not null default false,
  add column if not exists water_green_led  boolean     not null default false,
  add column if not exists soil_red_led     boolean     not null default false,
  add column if not exists soil_green_led   boolean     not null default false,
  add column if not exists buzzer_on        boolean     not null default false,
  add column if not exists is_simulated     boolean     not null default false,
  add column if not exists rssi             integer,
  add column if not exists uptime_s         integer,
  add column if not exists created_at       timestamptz not null default now();


-- Compatibility with the user's earlier sensor_readings table.
-- The previous schema had these NOT NULL columns. Defaults allow the new backend
-- to insert its normalized columns without violating the older constraints.
alter table public.sensor_readings
  alter column soil_raw_value set default 0,
  alter column soil_threshold_status set default 'NORMAL',
  alter column water_level_status set default 'HIGH',
  alter column buzzer_alert_status set default 'INACTIVE';

create index if not exists sensor_readings_device_time_idx
  on public.sensor_readings (device_id, created_at desc);

-- Alert history (one row per alert raised / cleared)
create table if not exists public.irrigation_alerts (
  id            bigint generated always as identity primary key,
  device_id     text        not null,
  alert_type    text        not null check (alert_type in ('water_low','soil_dry','device_offline')),
  state         text        not null check (state in ('raised','cleared')),
  severity      text        not null default 'info',
  message       text        not null,
  is_simulated  boolean     not null default false,
  created_at    timestamptz not null default now()
);
create index if not exists irrigation_alerts_device_time_idx
  on public.irrigation_alerts (device_id, created_at desc);

-- Admin-editable settings (per device)
create table if not exists public.irrigation_settings (
  device_id              text primary key,
  soil_threshold         integer check (soil_threshold between 0 and 4095),
  offline_after_seconds  integer check (offline_after_seconds between 10 and 3600),
  updated_at             timestamptz not null default now()
);

-- Aggregation used by the history charts (keeps the API fast for 24h / 7d ranges)
create or replace function public.sensor_history_buckets(p_device text, p_since timestamptz, p_bucket_seconds integer)
returns table (bucket timestamptz, n bigint, soil_avg double precision, soil_min integer, soil_max integer,
               water_low_ratio double precision, soil_dry_ratio double precision)
language sql stable security invoker set search_path = public
as $$
  select to_timestamp(floor(extract(epoch from created_at) / p_bucket_seconds) * p_bucket_seconds),
         count(*),
         avg(soil_value)::double precision,
         min(soil_value)::integer,
         max(soil_value)::integer,
         avg(case when water_low then 1.0 else 0.0 end)::double precision,
         avg(case when soil_dry  then 1.0 else 0.0 end)::double precision
  from public.sensor_readings
  where device_id = p_device and created_at >= p_since and soil_value is not null
  group by 1 order by 1
$$;
