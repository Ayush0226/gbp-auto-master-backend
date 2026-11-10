-- Cover new referencing columns without changing rows, roles or policies.
begin;
set local lock_timeout='3s';
create index if not exists access_redemptions_code_cover_idx on public.access_code_redemptions(code_hash);
create index if not exists image_generations_asset_cover_idx on public.image_generations(asset_id);
create index if not exists operation_runs_location_cover_idx on public.operation_runs(location_id);
create index if not exists operation_runs_user_cover_idx on public.operation_runs(user_id);
create index if not exists provider_oauth_states_user_cover_idx on public.provider_oauth_states(user_id);
commit;
