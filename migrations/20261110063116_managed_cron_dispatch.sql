-- Configure dispatch infrastructure only. Schedules are activated separately,
-- after matching application deployment and delivery reconciliation.
begin;
create extension if not exists pg_cron with schema pg_catalog;
create extension if not exists pg_net with schema extensions;
-- New extension queues contain outbound authorization headers.
revoke all on net.http_request_queue,net._http_response from anon,authenticated;
create table public.cron_dispatches (
  request_id bigint primary key, job_key text not null, dispatched_at timestamptz not null default now(),
  responded_at timestamptz, status_code integer, timed_out boolean, error_message text
);
alter table public.cron_dispatches enable row level security;
revoke all on public.cron_dispatches from public,anon,authenticated;
grant select on public.cron_dispatches to service_role;
create index cron_dispatches_job_time_idx on public.cron_dispatches(job_key,dispatched_at desc);
create function public.dispatch_gbp_cron(p_job text) returns bigint
language plpgsql security definer set search_path=public,pg_catalog as $$
declare secret text; request bigint;
begin
  if p_job not in ('publish-scheduled','reply-reviews','scrub-calendar','monitor') then raise exception 'Unknown cron job'; end if;
  select decrypted_secret into secret from vault.decrypted_secrets where name='gbp_cron_secret' limit 1;
  if secret is null or secret='' then raise exception 'Cron secret is not configured in Vault'; end if;
  select net.http_get(url:='https://gbp-auto-master-backend-us.onrender.com/api/cron/'||p_job,
    headers:=jsonb_build_object('Authorization','Bearer '||secret),timeout_milliseconds:=300000) into request;
  insert into cron_dispatches(request_id,job_key) values(request,'/api/cron/'||p_job);
  return request;
end $$;
create function public.collect_gbp_cron_results() returns void
language plpgsql security definer set search_path=public,pg_catalog as $$
begin
  update cron_dispatches d set status_code=r.status_code,timed_out=r.timed_out,
    error_message=left(r.error_msg,500),responded_at=now()
    from net._http_response r where d.request_id=r.id and d.responded_at is null;
  update cron_dispatches set responded_at=now(),timed_out=true,error_message='Response missing after 10 minutes; inspect worker heartbeat'
    where responded_at is null and dispatched_at<now()-interval '10 minutes';
  delete from cron_dispatches where dispatched_at<now()-interval '30 days';
  delete from job_run_history where finished_at<now()-interval '30 days';
  delete from provider_oauth_states where expires_at<now()-interval '1 day';
  delete from cron.job_run_details where end_time<now()-interval '14 days';
end $$;
revoke all on function public.dispatch_gbp_cron(text),public.collect_gbp_cron_results() from public,anon,authenticated;
grant execute on function public.dispatch_gbp_cron(text),public.collect_gbp_cron_results() to service_role;
commit;
