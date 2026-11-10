-- Pass the cron secret through a prepared RPC parameter, never as a literal in
-- a scheduler command or a checked-in SQL file. Existing app grants are retained.
begin;
create function public.configure_gbp_cron_secret(p_secret text) returns void
language plpgsql security definer set search_path=public,pg_catalog as $$
declare secret_id uuid;
begin
  if p_secret is null or length(p_secret)<24 or length(p_secret)>4096 then raise exception 'A strong cron secret is required'; end if;
  select id into secret_id from vault.decrypted_secrets where name='gbp_cron_secret' limit 1;
  if secret_id is null then
    perform vault.create_secret(p_secret,'gbp_cron_secret','GBP Master authenticated scheduler');
  else
    perform vault.update_secret(secret_id,p_secret);
  end if;
end $$;
revoke all on function public.configure_gbp_cron_secret(text) from public,anon,authenticated;
grant execute on function public.configure_gbp_cron_secret(text) to service_role;
notify pgrst,'reload schema';
commit;
