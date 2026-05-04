// EconDelta PWA — Supabase config
// The anon key is PUBLIC by design — Supabase RLS policies restrict it to
// SELECT on metric_definitions, run_logs, metric_history. Service-role keys
// stay on ExonVPS only.
window.ED_SUPABASE_CONFIG = {
  url: 'https://ssbliukchgibjcjohibi.supabase.co',
  anonKey: 'sb_publishable__e3sSI8aamLznnB9uN_LLg_wCWOuape',
};
