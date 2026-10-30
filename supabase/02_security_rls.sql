-- Row Level Security. Safe to run more than once.
-- Model: the browser NEVER talks to Supabase. Only the Flask backend does, using the
-- service-role key (server-side secret). So no policy grants anything to anon/authenticated.

alter table public.sensor_readings    enable row level security;
alter table public.irrigation_alerts  enable row level security;
alter table public.irrigation_settings enable row level security;

-- Defence in depth: remove table privileges from the public-facing roles on the NEW tables.
revoke all on public.irrigation_alerts   from anon, authenticated;
revoke all on public.irrigation_settings from anon, authenticated;
-- sensor_readings: your existing policies/grants are left exactly as they are. If you do not
-- need the browser/anon key to read it directly (this project does not), tighten it with:
--   revoke all on public.sensor_readings from anon, authenticated;

revoke execute on function public.sensor_history_buckets(text, timestamptz, integer) from public, anon, authenticated;
grant  execute on function public.sensor_history_buckets(text, timestamptz, integer) to service_role;

-- Verify: all three should show rls_enabled = true
select relname, relrowsecurity as rls_enabled from pg_class
where oid in ('public.sensor_readings'::regclass, 'public.irrigation_alerts'::regclass, 'public.irrigation_settings'::regclass);
