-- OPTIONAL, read-only. Run first in Supabase -> SQL Editor to see what your existing table looks like.
select column_name, data_type, is_nullable, column_default
from information_schema.columns
where table_schema = 'public' and table_name = 'sensor_readings'
order by ordinal_position;

-- Existing Row Level Security state and policies (these are preserved, never dropped by this project):
select relname, relrowsecurity as rls_enabled from pg_class where oid = 'public.sensor_readings'::regclass;
select policyname, roles, cmd, qual from pg_policies where schemaname = 'public' and tablename = 'sensor_readings';
