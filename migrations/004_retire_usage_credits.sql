-- Apply after 001-003, before deploying the matching backend and UI.
-- Historical balances, ledgers and payment records are retained for reconciliation.
-- Active workflows have no balance, usage price, grant or debit.
begin;

create table if not exists public.operation_runs (
  operation_id text primary key,
  location_id text references public.location_profiles(location_id),
  user_id uuid not null references auth.users(id),
  status text not null check (status in ('running','completed','failed')),
  fingerprint text,
  result jsonb,
  updated_at timestamptz not null default now()
);
alter table public.operation_runs enable row level security;
revoke all on public.operation_runs from public,anon,authenticated;
grant all on public.operation_runs to service_role;

-- Migrate duplicate-delivery guards, including uncertain historical deliveries.
insert into public.operation_runs(operation_id,location_id,user_id,status,updated_at)
select operation_id,location_id,user_id,
  case status when 'reserved' then 'running' when 'completed' then 'completed' else 'failed' end,
  updated_at from public.token_operations
on conflict(operation_id) do nothing;

create or replace function public.claim_operation(p_operation text,p_location text,p_user uuid)
returns jsonb language plpgsql security definer set search_path=public as $$
begin
  perform pg_advisory_xact_lock(hashtextextended('operations:'||p_user::text,0));
  if not exists(select 1 from operation_runs where operation_id=p_operation) and (
    (select count(*) from operation_runs where user_id=p_user and status='running' and updated_at>now()-interval '5 minutes')>=4
    or (select count(*) from operation_runs where user_id=p_user and updated_at>now()-interval '10 seconds')>=8
  ) then return jsonb_build_object('success',false,'error','Too many simultaneous requests; wait briefly and retry'); end if;
  if p_location is not null and not exists(
    select 1 from location_profiles where location_id=p_location and user_id=p_user
  ) then raise exception 'Location ownership mismatch'; end if;
  insert into operation_runs(operation_id,location_id,user_id,status)
    values(p_operation,p_location,p_user,'running')
  on conflict(operation_id) do update set status='running',updated_at=now()
    where operation_runs.user_id=p_user
      and operation_runs.location_id is not distinct from p_location
      and operation_runs.status='failed';
  if not found then return jsonb_build_object('success',false,'error','Operation already running or completed'); end if;
  return jsonb_build_object('success',true);
end $$;

create function public.claim_result_operation(p_operation text,p_location text,p_user uuid,p_fingerprint text)
returns jsonb language plpgsql security definer set search_path=public as $$
declare r operation_runs;
begin
  perform pg_advisory_xact_lock(hashtextextended('operations:'||p_user::text,0));
  if not exists(select 1 from operation_runs where operation_id=p_operation) and (
    (select count(*) from operation_runs where user_id=p_user and status='running' and updated_at>now()-interval '5 minutes')>=4
    or (select count(*) from operation_runs where user_id=p_user and updated_at>now()-interval '10 seconds')>=8
  ) then return jsonb_build_object('success',false,'error','Too many simultaneous requests; wait briefly and retry'); end if;
  if not exists(select 1 from location_profiles where location_id=p_location and user_id=p_user) then raise exception 'Location ownership mismatch'; end if;
  insert into operation_runs(operation_id,location_id,user_id,status,fingerprint)
    values(p_operation,p_location,p_user,'running',p_fingerprint) on conflict do nothing;
  if found then return jsonb_build_object('success',true); end if;
  select * into r from operation_runs where operation_id=p_operation for update;
  if r.user_id<>p_user or r.location_id is distinct from p_location or r.fingerprint is distinct from p_fingerprint
    then return jsonb_build_object('success',false,'error','Request ID belongs to a different operation'); end if;
  if r.status='completed' and r.result is not null then
    return jsonb_build_object('success',true,'replayed',true,'result',r.result);
  end if;
  if r.status='failed' then
    update operation_runs set status='running',updated_at=now() where operation_id=p_operation;
    return jsonb_build_object('success',true);
  end if;
  return jsonb_build_object('success',false,'error','Delivery is running or requires reconciliation');
end $$;
create function public.complete_result_operation(p_operation text,p_result jsonb) returns void
language sql security definer set search_path=public as $$
  update operation_runs set status='completed',result=p_result,updated_at=now() where operation_id=p_operation and status='running';
$$;
revoke all on function public.claim_result_operation(text,text,uuid,text),public.complete_result_operation(text,jsonb) from public,anon,authenticated;
grant execute on function public.claim_result_operation(text,text,uuid,text),public.complete_result_operation(text,jsonb) to service_role;

create or replace function public.finish_operation(p_operation text,p_success boolean)
returns void language plpgsql security definer set search_path=public as $$
begin
  update operation_runs set status=case when p_success then 'completed' else 'failed' end,
    updated_at=now() where operation_id=p_operation and status='running';
end $$;

create or replace function public.ensure_account(p_user uuid)
returns jsonb language plpgsql security definer set search_path=public as $$
declare v_profile user_profiles;
begin
  insert into user_profiles(id,plan_type,tokens_balance,max_seo_keywords,seo_keywords,reply_length,demo_completed,onboarding_completed)
    values(p_user,'free',0,2,'{}','50-90',false,false) on conflict(id) do nothing;
  select * into v_profile from user_profiles where id=p_user;
  insert into account_subscriptions(user_id) values(p_user) on conflict(user_id) do nothing;
  return to_jsonb(v_profile)-'tokens_balance'-'last_daily_claim';
end $$;

-- Expiry remains meaningful even though monthly credit refills are retired.
update account_subscriptions set monthly_credit_allowance=0,next_credit_grant_at=null;
update location_profiles set monthly_token_allowance=0,next_token_grant_at=null;

create or replace function public.settle_order(p_order text,p_payment text,p_user uuid)
returns jsonb language plpgsql security definer set search_path=public as $$
declare v_order billing_orders; v_expires timestamptz;
begin
  select * into v_order from billing_orders where order_id=p_order and user_id=p_user for update;
  if not found then raise exception 'Unknown order'; end if;
  if v_order.processed_at is not null then return jsonb_build_object('status','success','already_processed',true); end if;
  if v_order.kind<>'subscription' then raise exception 'Usage-credit purchases have been retired; contact support for historical payments'; end if;
  if exists(select 1 from billing_orders where payment_id=p_payment) then raise exception 'Payment already used'; end if;
  perform ensure_account(p_user);
  select greatest(now(),coalesce(expires_at,now()))+make_interval(months=>v_order.duration_months)
    into v_expires from account_subscriptions where user_id=p_user for update;
  update account_subscriptions set plan_type=v_order.product_id,status='active',expires_at=v_expires,
    monthly_credit_allowance=0,next_credit_grant_at=null,auto_renew=false,updated_at=now() where user_id=p_user;
  update location_profiles set plan_type=v_order.product_id,subscription_end=v_expires,
    monthly_token_allowance=0,next_token_grant_at=null,auto_renew=false where user_id=p_user;
  update billing_orders set payment_id=p_payment,processed_at=now() where order_id=p_order;
  return jsonb_build_object('status','success','subscription_end',v_expires);
end $$;

create or replace function public.schedule_post(p_user uuid,p_location text,p_date date,p_caption text,p_image text,p_type text)
returns jsonb language plpgsql security definer set search_path = public as $$
declare v_result jsonb; v_id uuid := gen_random_uuid(); v_publish_at timestamptz;
begin
  if p_type not in ('LOCAL_POST','PHOTO') then raise exception 'Invalid post type'; end if;
  if not exists(select 1 from location_profiles where location_id=p_location and user_id=p_user) then
    raise exception 'Location ownership mismatch';
  end if;
  v_publish_at := p_date::timestamp at time zone 'Asia/Kolkata';
  insert into calendar_posts(id,user_id,location_id,post_date,publish_at,caption,image_url,post_type,status)
    values(v_id,p_user,p_location,p_date,v_publish_at,p_caption,p_image,p_type,'scheduled');
  return jsonb_build_object('status','success','id',v_id,'publish_at',v_publish_at);
end $$;

create or replace function public.schedule_post_at(p_user uuid,p_location text,p_publish_at timestamptz,p_caption text,p_image text,p_type text)
returns jsonb language plpgsql security definer set search_path = public as $$
declare v_result jsonb; v_id uuid := gen_random_uuid();
begin
  if p_publish_at is null or p_publish_at <= now() then raise exception 'Publication time must be in the future'; end if;
  if p_type not in ('LOCAL_POST','PHOTO') then raise exception 'Invalid post type'; end if;
  if coalesce(btrim(p_caption),'') = '' and p_image is null then raise exception 'Add text or media'; end if;
  if p_type in ('PHOTO','VIDEO') and p_image is null then raise exception 'Media URL required'; end if;
  if not exists(select 1 from location_profiles where location_id=p_location and user_id=p_user) then
    raise exception 'Location ownership mismatch';
  end if;
  insert into calendar_posts(id,user_id,location_id,post_date,publish_at,caption,image_url,post_type,status)
    values(v_id,p_user,p_location,(p_publish_at at time zone 'Asia/Kolkata')::date,p_publish_at,p_caption,p_image,p_type,'scheduled');
  return jsonb_build_object('status','success','id',v_id,'publish_at',p_publish_at);
end $$;

create or replace function public.schedule_content_campaign(p_user uuid,p_campaign uuid,p_publish_at timestamptz)
returns jsonb language plpgsql security definer set search_path=public as $$
declare v_campaign content_campaigns; v_count integer; v_cost numeric; v_result jsonb;
begin
  select * into v_campaign from content_campaigns where id=p_campaign and user_id=p_user for update;
  if not found then raise exception 'Campaign not found'; end if;
  if v_campaign.status not in ('draft','awaiting_approval') then raise exception 'Campaign cannot be scheduled from its current status'; end if;
  if p_publish_at is null or p_publish_at<=now() then raise exception 'Publication time must be in the future'; end if;
  select count(*) into v_count from campaign_locations where campaign_id=p_campaign and user_id=p_user;
  if v_count=0 then raise exception 'Campaign has no locations'; end if;
  update content_campaigns set status='scheduled',scheduled_for=p_publish_at,updated_at=now() where id=p_campaign;
  update campaign_locations set status='scheduled',scheduled_for=p_publish_at,updated_at=now() where campaign_id=p_campaign;
  insert into audit_events(user_id,actor_type,action,resource_type,resource_id,detail)
    values(p_user,'user','schedule','content_campaign',p_campaign::text,jsonb_build_object('locations',v_count,'publish_at',p_publish_at));
  return jsonb_build_object('status','scheduled','campaign_id',p_campaign,'location_count',v_count,'publish_at',p_publish_at);
end $$;

create or replace function public.cancel_content_campaign(p_user uuid,p_campaign uuid)
returns jsonb language plpgsql security definer set search_path=public as $$
declare v_campaign content_campaigns;
begin
  select * into v_campaign from content_campaigns where id=p_campaign and user_id=p_user for update;
  if not found then raise exception 'Campaign not found'; end if;
  if v_campaign.status not in ('draft','awaiting_approval','scheduled') then raise exception 'Only an unpublished campaign can be cancelled'; end if;
  update content_campaigns set status='cancelled',updated_at=now() where id=p_campaign;
  update campaign_locations set status='cancelled',updated_at=now() where campaign_id=p_campaign and status in ('draft','scheduled');
  insert into audit_events(user_id,actor_type,action,resource_type,resource_id,detail)
    values(p_user,'user','cancel','content_campaign',p_campaign::text,'{}'::jsonb);
  return jsonb_build_object('status','cancelled','campaign_id',p_campaign);
end $$;

create or replace function public.register_location(p_location text, p_user uuid, p_account text)
returns jsonb language plpgsql security definer set search_path = public as $$
declare v_profile location_profiles; v_sub jsonb; v_end timestamptz; v_plan text;
begin
  perform pg_advisory_xact_lock(hashtextextended(p_user::text, 0));
  select * into v_profile from location_profiles where location_id = p_location for update;
  if found then
    if v_profile.user_id <> p_user then raise exception 'Location already belongs to another account'; end if;
    update location_profiles set account_id=p_account where location_id=p_location;
    return to_jsonb(v_profile);
  end if;
  -- Linking a business profile never changes the account's token balance.
  insert into location_profiles(location_id,user_id,account_id)
    values(p_location,p_user,p_account) returning * into v_profile;
  select subscription into v_sub from legacy_subscription_snapshot where user_id=p_user and location_id=p_location;
  if v_sub is not null and v_sub->>'status'='active' then
    begin
      v_end := (v_sub->>'expires_at')::timestamptz;
      v_plan := case v_sub->>'plan_id' when 'half' then 'half_yearly' when 'annual' then 'yearly' else v_sub->>'plan_id' end;
      if v_end>now() and v_plan in ('monthly','half_yearly','yearly') then
        update location_profiles set plan_type=v_plan,subscription_end=v_end,
          next_token_grant_at=null, monthly_token_allowance=0
          where location_id=p_location returning * into v_profile;
      end if;
    exception when invalid_datetime_format or datetime_field_overflow then
      -- Malformed historical entries remain in the snapshot for manual review.
      null;
    end;
  end if;
  return to_jsonb(v_profile);
end $$;

-- Stop legacy callers from changing balances or reactivating retired sales.
revoke all on function public.change_tokens(text,uuid,numeric,text,text,text),
  public.reserve_tokens(text,text,uuid,numeric),public.finish_tokens(text,boolean),
  public.refresh_monthly_tokens(text,uuid) from public,anon,authenticated,service_role;
-- Preserve historical ledger visibility and existing user permissions. Active
-- billing actions are retired by the server-only RPC revocations above.
revoke all on function public.claim_operation(text,text,uuid),public.finish_operation(text,boolean)
  from public,anon,authenticated;
grant execute on function public.claim_operation(text,text,uuid),public.finish_operation(text,boolean) to service_role;
-- Existing scheduling and settlement functions retain their service-role grants.
notify pgrst, 'reload schema';
commit;
