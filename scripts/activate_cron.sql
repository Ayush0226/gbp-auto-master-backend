-- Operational activation after deployment, grant migration and reconciliation.
-- No user, permission, ownership, or subscription changes.
begin;
do $$
begin
  if exists(select 1 from public.job_runs where job_key in
    ('/api/cron/publish-scheduled','/api/cron/reply-reviews','/api/cron/scrub-calendar') and status<>'completed')
    or (select count(*) from public.job_runs where job_key in
    ('/api/cron/publish-scheduled','/api/cron/reply-reviews','/api/cron/scrub-calendar'))<>3
    then raise exception 'Complete controlled recovery runs before activation'; end if;
  if exists(select 1 from public.calendar_posts where status='publishing')
    or exists(select 1 from public.campaign_locations where status='publishing')
    or exists(select 1 from public.review_reply_jobs where status='publishing')
    then raise exception 'Reconcile held deliveries before activation'; end if;
  if not exists(select 1 from vault.decrypted_secrets where name='gbp_cron_secret' and length(decrypted_secret)>=24)
    then raise exception 'Configure the protected cron secret first'; end if;
end $$;
select cron.schedule('gbp-publish-scheduled','*/5 * * * *', $$select public.dispatch_gbp_cron('publish-scheduled');$$);
select cron.schedule('gbp-reply-reviews','2-59/10 * * * *', $$select public.dispatch_gbp_cron('reply-reviews');$$);
select cron.schedule('gbp-scrub-calendar','30 20 * * *', $$select public.dispatch_gbp_cron('scrub-calendar');$$);
select cron.schedule('gbp-monitor','4-59/10 * * * *', $$select public.dispatch_gbp_cron('monitor');$$);
select cron.schedule('gbp-collect-results','* * * * *', $$select public.collect_gbp_cron_results();$$);
commit;
