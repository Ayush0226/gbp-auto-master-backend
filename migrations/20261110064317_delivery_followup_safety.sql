-- Preserve rule changes and hold stale content at the publication boundary.
begin;
create or replace function public.claim_review_delivery(p_job uuid) returns boolean
language plpgsql security definer set search_path=public as $$
declare j review_reply_jobs; r review_automation_rules; tz text; d date; n integer;
begin
  select * into j from review_reply_jobs where id=p_job;
  if not found then return false; end if;
  if j.rule_id is not null then
    select * into r from review_automation_rules where id=j.rule_id for update;
    if not r.enabled then return false; end if;
    if r.mode<>'auto_publish' and j.approval_source='automation' then
      update review_reply_jobs set status='pending_approval',last_error='Automatic publishing was paused or changed to approval',updated_at=now()
        where id=p_job and status='scheduled';
      return false;
    end if;
  end if;
  select * into j from review_reply_jobs where id=p_job for update;
  if j.status<>'scheduled' or j.scheduled_for>now() then return false; end if;
  select timezone into tz from account_preferences where user_id=j.user_id;
  tz:=coalesce(tz,'Asia/Kolkata');
  if not exists(select 1 from pg_timezone_names where name=tz) then tz:='Asia/Kolkata'; end if;
  d:=(now() at time zone tz)::date;
  if j.rule_id is not null and j.approval_source='automation' then
    select count(*) into n from review_reply_jobs where rule_id=j.rule_id
      and publication_day=d and status in ('publishing','published');
    if n>=r.daily_limit then
      update review_reply_jobs set status='pending_approval',last_error='Daily automatic publication limit reached',updated_at=now() where id=p_job;
      return false;
    end if;
  end if;
  update review_reply_jobs set status='publishing',publication_day=d,attempt_count=attempt_count+1,last_error=null,updated_at=now() where id=p_job;
  return true;
end $$;
create or replace function public.claim_campaign_delivery(p_campaign uuid,p_location text) returns boolean
language plpgsql security definer set search_path=public as $$
declare c content_campaigns;
begin
  select * into c from content_campaigns where id=p_campaign for update;
  if not found or c.status not in ('scheduled','processing','partially_published') then return false; end if;
  if exists(select 1 from campaign_locations where campaign_id=p_campaign and location_id=p_location
      and status='scheduled' and scheduled_for<now()-interval '1 hour') then
    update campaign_locations set status='failed',last_error='Overdue post requires owner review before rescheduling',updated_at=now()
      where campaign_id=p_campaign and location_id=p_location and status='scheduled';
    return false;
  end if;
  update campaign_locations set status='publishing',attempt_count=attempt_count+1,last_error=null,updated_at=now()
    where campaign_id=p_campaign and location_id=p_location and status='scheduled' and scheduled_for<=now();
  if not found then return false; end if;
  update content_campaigns set status='processing',updated_at=now() where id=p_campaign;
  return true;
end $$;
commit;
