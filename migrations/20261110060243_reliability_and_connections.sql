-- Additive release: never changes users, roles, existing RLS policies or owners.
begin;
alter table public.job_runs add column if not exists lease_owner uuid;
alter table public.job_runs add column if not exists lease_expires_at timestamptz;
alter table public.job_runs add column if not exists started_at timestamptz;
create table public.job_run_history (
  run_id uuid primary key, job_key text not null, started_at timestamptz not null default now(),
  finished_at timestamptz, status text not null default 'running', detail text
);
alter table public.job_run_history enable row level security;
revoke all on public.job_run_history from public,anon,authenticated;
grant all on public.job_run_history to service_role;

create function public.claim_job_lease(p_key text,p_owner uuid) returns boolean
language plpgsql security definer set search_path=public as $$
declare old_owner uuid;
begin
  insert into job_runs(job_key,status) values(p_key,'idle') on conflict do nothing;
  select lease_owner into old_owner from job_runs where job_key=p_key for update;
  -- A pre-lease running job requires explicit reconciliation. Do not release it.
  if exists(select 1 from job_runs where job_key=p_key and status='running'
    and (lease_expires_at is null or lease_expires_at>now())) then return false; end if;
  update job_run_history set status='abandoned',finished_at=now(),detail='Lease expired; inspect held deliveries'
    where run_id=old_owner and status='running';
  update job_runs set status='running',lease_owner=p_owner,lease_expires_at=now()+interval '5 minutes',
    started_at=now(),updated_at=now(),detail=null where job_key=p_key;
  insert into job_run_history(run_id,job_key) values(p_owner,p_key);
  return true;
end $$;
create function public.heartbeat_job_lease(p_key text,p_owner uuid) returns boolean
language plpgsql security definer set search_path=public as $$
begin
  update job_runs set updated_at=now(),lease_expires_at=now()+interval '5 minutes'
    where job_key=p_key and lease_owner=p_owner and status='running' and lease_expires_at>now();
  return found;
end $$;
create function public.finish_job_lease(p_key text,p_owner uuid,p_status text) returns boolean
language plpgsql security definer set search_path=public as $$
begin
  if p_status not in ('completed','failed') then raise exception 'Invalid result'; end if;
  update job_runs set status=p_status,updated_at=now(),lease_expires_at=null,
    detail=case when p_status='failed' then 'Worker failed; inspect logs and delivery status' end
    where job_key=p_key and lease_owner=p_owner and status='running';
  if not found then return false; end if;
  update job_run_history set status=p_status,finished_at=now() where run_id=p_owner;
  return true;
end $$;

-- Both cancellation and delivery claims lock the same parent first.
create function public.claim_campaign_delivery(p_campaign uuid,p_location text) returns boolean
language plpgsql security definer set search_path=public as $$
declare c content_campaigns;
begin
  select * into c from content_campaigns where id=p_campaign for update;
  if not found or c.status not in ('scheduled','processing','partially_published') then return false; end if;
  update campaign_locations set status='publishing',attempt_count=attempt_count+1,last_error=null,updated_at=now()
    where campaign_id=p_campaign and location_id=p_location and status='scheduled' and scheduled_for<=now();
  if not found then return false; end if;
  update content_campaigns set status='processing',updated_at=now() where id=p_campaign;
  return true;
end $$;
create or replace function public.cancel_content_campaign(p_user uuid,p_campaign uuid) returns jsonb
language plpgsql security definer set search_path=public as $$
declare c content_campaigns;
begin
  select * into c from content_campaigns where id=p_campaign and user_id=p_user for update;
  if not found then raise exception 'Campaign not found'; end if;
  if c.status='cancelled' then return jsonb_build_object('status','cancelled','campaign_id',p_campaign); end if;
  if c.status not in ('draft','awaiting_approval','scheduled') or exists(
    select 1 from campaign_locations where campaign_id=p_campaign and status in ('publishing','published')
  ) then raise exception 'Delivery has started; reconcile it before cancelling'; end if;
  update content_campaigns set status='cancelled',updated_at=now() where id=p_campaign;
  update campaign_locations set status='cancelled',updated_at=now() where campaign_id=p_campaign and status in ('draft','scheduled','failed');
  insert into audit_events(user_id,actor_type,action,resource_type,resource_id)
    values(p_user,'user','cancel','content_campaign',p_campaign::text);
  return jsonb_build_object('status','cancelled','campaign_id',p_campaign);
end $$;

-- Existing ownership is immutable. A shared Google account cannot be reassigned
-- as a side effect of connection or synchronization.
create function public.sync_gbp_account(p_user uuid,p_resource text,p_data jsonb) returns boolean
language plpgsql security definer set search_path=public as $$
begin
  insert into gbp_accounts(resource_name,user_id,display_name,account_type,role,verification_state,last_synced_at)
    values(p_resource,p_user,coalesce(p_data->>'display_name','Google Business Profile account'),
      p_data->>'account_type',p_data->>'role',p_data->>'verification_state',now())
  on conflict(resource_name) do update set display_name=excluded.display_name,
    account_type=excluded.account_type,role=excluded.role,verification_state=excluded.verification_state,
    last_synced_at=now(),updated_at=now() where gbp_accounts.user_id=excluded.user_id;
  return found;
end $$;
create or replace function public.sync_location_gbp_account() returns trigger
language plpgsql security definer set search_path=public as $$
begin
  insert into gbp_accounts(resource_name,user_id,last_synced_at) values(new.account_id,new.user_id,now())
    on conflict(resource_name) do update set last_synced_at=now(),updated_at=now()
      where gbp_accounts.user_id=excluded.user_id;
  if not found then raise exception 'Google account belongs to another application user; ownership was preserved'; end if;
  return new;
end $$;

create table public.provider_credentials (
  user_id uuid not null references auth.users(id) on delete cascade,
  provider text not null check(provider in ('google','pollinations')),
  ciphertext text not null, subject text, expires_at timestamptz,
  updated_at timestamptz not null default now(), primary key(user_id,provider)
);
create table public.provider_oauth_states (
  state_hash text primary key, user_id uuid not null references auth.users(id) on delete cascade,
  verifier_ciphertext text not null, expires_at timestamptz not null
);
create table public.image_generations (
  user_id uuid not null references auth.users(id) on delete cascade, request_id uuid not null,
  fingerprint text not null, status text not null check(status in ('running','completed','failed')),
  asset_id uuid references media_assets(id), created_at timestamptz not null default now(),
  primary key(user_id,request_id)
);
create function public.claim_image_generation(p_user uuid,p_request uuid,p_fingerprint text) returns boolean
language plpgsql security definer set search_path=public as $$
begin
  perform pg_advisory_xact_lock(hashtextextended(p_user::text,17));
  if exists(select 1 from image_generations where user_id=p_user and request_id=p_request) then return false; end if;
  if exists(select 1 from image_generations where user_id=p_user and created_at>now()-interval '10 seconds')
    or exists(select 1 from image_generations where user_id=p_user and status='running' and created_at>now()-interval '3 minutes')
    then raise exception 'An image is already being generated; wait before starting another'; end if;
  insert into image_generations(user_id,request_id,fingerprint,status) values(p_user,p_request,p_fingerprint,'running');
  return true;
end $$;
alter table public.provider_credentials enable row level security;
alter table public.provider_oauth_states enable row level security;
alter table public.image_generations enable row level security;
revoke all on public.provider_credentials,public.provider_oauth_states,public.image_generations from public,anon,authenticated;
grant all on public.provider_credentials,public.provider_oauth_states,public.image_generations to service_role;

-- A claim locks the effective rule. Paused rules stop queued automatic replies.
alter table public.review_reply_jobs add column if not exists approval_source text not null default 'automation';
alter table public.review_reply_jobs add column if not exists publication_day date;
create function public.enqueue_review_reply(p_job jsonb) returns jsonb
language plpgsql security definer set search_path=public as $$
declare j review_reply_jobs;
begin
  insert into review_reply_jobs(event_id,user_id,location_id,rule_id,review_name,rating,review_text,draft_text,status,scheduled_for,last_error)
    values(p_job->>'event_id',(p_job->>'user_id')::uuid,p_job->>'location_id',(p_job->>'rule_id')::uuid,
      p_job->>'review_name',(p_job->>'rating')::integer,p_job->>'review_text',p_job->>'draft_text',
      p_job->>'status',(p_job->>'scheduled_for')::timestamptz,p_job->>'last_error')
    on conflict(user_id,review_name) do nothing;
  select * into j from review_reply_jobs where user_id=(p_job->>'user_id')::uuid and review_name=p_job->>'review_name';
  return to_jsonb(j);
end $$;
create function public.claim_review_delivery(p_job uuid) returns boolean
language plpgsql security definer set search_path=public as $$
declare j review_reply_jobs; r review_automation_rules; tz text; d date; n integer;
begin
  select * into j from review_reply_jobs where id=p_job;
  if not found then return false; end if;
  if j.rule_id is not null then
    select * into r from review_automation_rules where id=j.rule_id for update;
    if not r.enabled then return false; end if;
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

revoke all on function public.claim_job_lease(text,uuid),public.heartbeat_job_lease(text,uuid),
  public.finish_job_lease(text,uuid,text),public.claim_campaign_delivery(uuid,text),
  public.sync_gbp_account(uuid,text,jsonb),public.claim_review_delivery(uuid),public.enqueue_review_reply(jsonb),public.claim_image_generation(uuid,uuid,text) from public,anon,authenticated;
grant execute on function public.claim_job_lease(text,uuid),public.heartbeat_job_lease(text,uuid),
  public.finish_job_lease(text,uuid,text),public.claim_campaign_delivery(uuid,text),
  public.sync_gbp_account(uuid,text,jsonb),public.claim_review_delivery(uuid),public.enqueue_review_reply(jsonb),public.claim_image_generation(uuid,uuid,text) to service_role;
notify pgrst,'reload schema';
commit;
