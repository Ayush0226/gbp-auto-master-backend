-- Run once in the Supabase SQL editor before deploying the accompanying backend.
-- Additive migration: existing profiles, balances and metadata are retained.
begin;

create table if not exists public.location_profiles (
  location_id text primary key,
  user_id uuid not null references auth.users(id),
  account_id text not null,
  plan_type text not null default 'free',
  subscription_end timestamptz,
  next_token_grant_at timestamptz,
  monthly_token_allowance numeric not null default 0,
  auto_renew boolean not null default false,
  updated_at timestamptz not null default now()
);
create table if not exists public.account_token_ledger (
  id bigint generated always as identity primary key,
  location_id text references public.location_profiles(location_id),
  user_id uuid not null references auth.users(id),
  amount numeric not null,
  action_type text not null,
  description text not null default '',
  reference_id text unique,
  created_at timestamptz not null default now()
);
-- Preserve the pre-cutover subscription snapshot, then stop trusting editable metadata.
-- This carries existing entitlements forward; it does not certify historical payments.
create table if not exists public.legacy_subscription_snapshot (
  user_id uuid not null references auth.users(id),
  location_id text not null,
  subscription jsonb not null,
  primary key(user_id,location_id)
);
insert into public.legacy_subscription_snapshot(user_id,location_id,subscription)
select u.id, 'locations/' || regexp_replace(entry.key, '^.*locations/', ''), entry.value
from auth.users u cross join lateral jsonb_each(
  case when jsonb_typeof(u.raw_user_meta_data->'subscriptions')='object'
  then u.raw_user_meta_data->'subscriptions' else '{}'::jsonb end) entry
where entry.key ~ '^(accounts/[A-Za-z0-9_-]+/)?(locations/)?[A-Za-z0-9_-]+$'
on conflict do nothing;
alter table public.legacy_subscription_snapshot enable row level security;
revoke all on public.legacy_subscription_snapshot from anon,authenticated;
grant all on public.legacy_subscription_snapshot to service_role;
-- Preserve the project's two existing operator identities in server-owned metadata.
update auth.users set raw_app_meta_data=coalesce(raw_app_meta_data,'{}'::jsonb)||'{"role":"admin"}'::jsonb
where lower(email) in ('ayushsony126@gmail.com','aryansoni12567@gmail.com') and email_confirmed_at is not null;
create table if not exists public.billing_orders (
  order_id text primary key,
  user_id uuid not null references auth.users(id),
  location_id text references public.location_profiles(location_id),
  kind text not null check (kind in ('subscription', 'topup', 'promo')),
  product_id text not null,
  amount integer not null check (amount >= 0),
  tokens numeric not null check (tokens >= 0),
  duration_months integer not null default 0,
  payment_id text unique,
  processed_at timestamptz,
  created_at timestamptz not null default now()
);
create table if not exists public.token_operations (
  operation_id text primary key,
  location_id text references public.location_profiles(location_id),
  user_id uuid not null references auth.users(id),
  amount numeric not null check (amount > 0),
  status text not null check (status in ('reserved', 'completed', 'refunded')),
  updated_at timestamptz not null default now()
);
create table if not exists public.job_runs (
  job_key text primary key,
  status text not null,
  updated_at timestamptz not null default now(),
  detail text
);

-- Existing user_profiles.tokens_balance remains the single source of truth.
-- Copy history only: do not replay credits or grants into the current balance.
insert into public.account_token_ledger(user_id,amount,action_type,description,reference_id,created_at)
select user_id,amount,action,coalesce(description,''),'legacy:'||id::text,created_at
from public.token_ledger on conflict(reference_id) do nothing;

create or replace function public.ensure_account(p_user uuid)
returns jsonb language plpgsql security definer set search_path = public as $$
declare v_profile user_profiles;
begin
  insert into user_profiles(id,plan_type,tokens_balance,max_seo_keywords,seo_keywords,reply_length,demo_completed,onboarding_completed)
    values(p_user,'free',200,2,'{}','50-90',false,false) on conflict(id) do nothing returning * into v_profile;
  if found then
    insert into account_token_ledger(user_id,amount,action_type,description,reference_id)
      values(p_user,200,'signup_bonus','One-time account signup bonus','signup:'||p_user::text);
  else
    select * into v_profile from user_profiles where id=p_user;
  end if;
  return to_jsonb(v_profile);
end $$;
revoke all on function public.ensure_account(uuid) from public,anon,authenticated;
grant execute on function public.ensure_account(uuid) to service_role;

-- Clients may read their own balances, but cannot mint credits or edit plans.
alter table public.location_profiles enable row level security;
alter table public.account_token_ledger enable row level security;
alter table public.billing_orders enable row level security;
alter table public.token_operations enable row level security;
alter table public.job_runs enable row level security;
revoke all on public.location_profiles, public.account_token_ledger,
 public.billing_orders, public.token_operations,
 public.job_runs from anon, authenticated;
grant select on public.location_profiles, public.account_token_ledger to authenticated;
create policy location_owner_read on public.location_profiles for select to authenticated using (auth.uid() = user_id);
create policy ledger_owner_read on public.account_token_ledger for select to authenticated using (auth.uid() = user_id);
-- Legacy balances must not be writable by browser clients either.
revoke insert, update, delete on public.user_profiles, public.token_ledger, public.user_settings from anon, authenticated;

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
          next_token_grant_at=now()+interval '1 month',
          monthly_token_allowance=case v_plan when 'monthly' then 350 when 'half_yearly' then 600 else 750 end
          where location_id=p_location returning * into v_profile;
      end if;
    exception when invalid_datetime_format or datetime_field_overflow then
      -- Malformed historical entries remain in the snapshot for manual review.
      null;
    end;
  end if;
  return to_jsonb(v_profile);
end $$;

create or replace function public.change_tokens(p_location text,p_user uuid,p_amount numeric,p_action text,p_description text,p_reference text default null)
returns jsonb language plpgsql security definer set search_path = public as $$
declare v_balance numeric;
begin
  if p_location is not null and not exists(select 1 from location_profiles where location_id=p_location and user_id=p_user) then
    raise exception 'Location ownership mismatch';
  end if;
  select coalesce(tokens_balance,0) into v_balance from user_profiles where id=p_user for update;
  if not found then raise exception 'Account not initialized'; end if;
  if p_reference is not null and exists(select 1 from account_token_ledger where reference_id=p_reference) then
    return jsonb_build_object('success',true,'balance',v_balance,'duplicate',true);
  end if;
  if v_balance+p_amount < 0 then return jsonb_build_object('success',false,'balance',v_balance,'error','Insufficient tokens'); end if;
  update user_profiles set tokens_balance=coalesce(tokens_balance,0)+p_amount,updated_at=now() where id=p_user;
  insert into account_token_ledger(location_id,user_id,amount,action_type,description,reference_id)
    values(p_location,p_user,p_amount,p_action,p_description,p_reference);
  return jsonb_build_object('success',true,'balance',v_balance+p_amount);
end $$;

create or replace function public.settle_order(p_order text,p_payment text,p_user uuid)
returns jsonb language plpgsql security definer set search_path = public as $$
declare v_order billing_orders; v_result jsonb;
begin
  select * into v_order from billing_orders where order_id=p_order and user_id=p_user for update;
  if not found then raise exception 'Unknown order'; end if;
  if v_order.processed_at is not null then return jsonb_build_object('status','success','already_processed',true); end if;
  if exists(select 1 from billing_orders where payment_id=p_payment) then raise exception 'Payment already used'; end if;
  v_result := change_tokens(v_order.location_id,p_user,v_order.tokens,v_order.kind||'_purchase','Order '||p_order,'order:'||p_order);
  if v_order.kind='subscription' then
    update location_profiles set plan_type=v_order.product_id,
      subscription_end=now()+make_interval(months=>v_order.duration_months),auto_renew=false,
      monthly_token_allowance=v_order.tokens,next_token_grant_at=now()+interval '1 month'
      where location_id=v_order.location_id and user_id=p_user;
  end if;
  update billing_orders set payment_id=p_payment,processed_at=now() where order_id=p_order;
  return v_result || jsonb_build_object('status','success','tokens_added',v_order.tokens);
end $$;

create or replace function public.reserve_tokens(p_operation text,p_location text,p_user uuid,p_amount numeric)
returns jsonb language plpgsql security definer set search_path = public as $$
declare v_operation token_operations; v_result jsonb;
begin
  perform pg_advisory_xact_lock(hashtextextended(p_operation, 0));
  select * into v_operation from token_operations where operation_id=p_operation;
  if found and (v_operation.user_id<>p_user or v_operation.location_id is distinct from p_location or v_operation.amount<>p_amount) then
    raise exception 'Operation does not match its original reservation';
  end if;
  if found and v_operation.status <> 'refunded' then return jsonb_build_object('success',false,'error','Operation already reserved or completed'); end if;
  v_result := change_tokens(p_location,p_user,-p_amount,'reservation',p_operation,null);
  if not (v_result->>'success')::boolean then return v_result; end if;
  insert into token_operations(operation_id,location_id,user_id,amount,status) values(p_operation,p_location,p_user,p_amount,'reserved')
    on conflict(operation_id) do update set status='reserved',updated_at=now();
  return v_result;
end $$;

create or replace function public.refresh_monthly_tokens(p_location text,p_user uuid)
returns void language plpgsql security definer set search_path = public as $$
declare v_profile location_profiles; v_due timestamptz;
begin
  perform 1 from user_profiles where id=p_user for update;
  select * into v_profile from location_profiles where location_id=p_location and user_id=p_user for update;
  if not found then raise exception 'Location ownership mismatch'; end if;
  v_due := v_profile.next_token_grant_at;
  while v_due <= now() and v_due < v_profile.subscription_end and v_profile.monthly_token_allowance > 0 loop
    perform change_tokens(p_location,p_user,v_profile.monthly_token_allowance,'monthly_grant','Monthly plan tokens','monthly:'||p_location||':'||v_due::text);
    v_due := v_due + interval '1 month';
  end loop;
  update location_profiles set next_token_grant_at=v_due where location_id=p_location;
end $$;
revoke all on function public.refresh_monthly_tokens(text,uuid) from public,anon,authenticated;
grant execute on function public.refresh_monthly_tokens(text,uuid) to service_role;

create or replace function public.finish_tokens(p_operation text,p_success boolean)
returns void language plpgsql security definer set search_path = public as $$
declare v_operation token_operations;
begin
  select * into v_operation from token_operations where operation_id=p_operation for update;
  if not found or v_operation.status <> 'reserved' then return; end if;
  if not p_success then
    perform change_tokens(v_operation.location_id,v_operation.user_id,v_operation.amount,'refund',p_operation,null);
  end if;
  update token_operations set status=case when p_success then 'completed' else 'refunded' end,updated_at=now() where operation_id=p_operation;
end $$;

create or replace function public.claim_job(p_key text)
returns boolean language plpgsql security definer set search_path = public as $$
begin
  insert into job_runs(job_key,status) values(p_key,'running') on conflict(job_key) do nothing;
  if found then return true; end if;
  update job_runs set status='running',updated_at=now(),detail=null
    where job_key=p_key and status <> 'running';
  return found;
end $$;
revoke all on function public.claim_job(text) from public,anon,authenticated;
grant execute on function public.claim_job(text) to service_role;

create or replace function public.schedule_post(p_user uuid,p_location text,p_date date,p_caption text,p_image text,p_type text)
returns jsonb language plpgsql security definer set search_path = public as $$
declare v_result jsonb; v_id uuid := gen_random_uuid();
begin
  v_result := change_tokens(p_location,p_user,-5,'schedule_post','Scheduled business post','schedule:'||v_id::text);
  if not (v_result->>'success')::boolean then raise exception 'Insufficient tokens'; end if;
  insert into calendar_posts(id,user_id,location_id,post_date,caption,image_url,post_type,status)
    values(v_id,p_user,p_location,p_date,p_caption,p_image,p_type,'scheduled');
  return jsonb_build_object('status','success','id',v_id,'balance',v_result->'balance');
end $$;
revoke all on function public.schedule_post(uuid,text,date,text,text,text) from public,anon,authenticated;
grant execute on function public.schedule_post(uuid,text,date,text,text,text) to service_role;

-- Existing calendar policies may be permissive; restrictive policies enforce ownership.
alter table public.calendar_posts enable row level security;
create policy calendar_owner_guard on public.calendar_posts as restrictive for all to authenticated
  using (user_id=auth.uid()) with check (user_id=auth.uid() and exists (
    select 1 from public.location_profiles l where l.location_id=calendar_posts.location_id and l.user_id=auth.uid()));
create policy calendar_owner_access on public.calendar_posts for select to authenticated using(user_id=auth.uid());
-- All calendar writes now go through the backend, where token charges are enforced.
revoke insert,update,delete on public.calendar_posts from anon,authenticated;
revoke all on public.calendar_posts from anon;
alter table public.user_profiles enable row level security;
alter table public.token_ledger enable row level security;
alter table public.user_settings enable row level security;
revoke all on public.user_profiles,public.token_ledger,public.user_settings,public.locations,public.scheduled_posts from anon;
create policy user_profile_owner_guard on public.user_profiles as restrictive for select to authenticated using (id=auth.uid());
create policy user_profile_owner_read on public.user_profiles for select to authenticated using(id=auth.uid());
create policy legacy_ledger_owner_guard on public.token_ledger as restrictive for select to authenticated using(user_id=auth.uid());
create policy settings_owner_guard on public.user_settings as restrictive for select to authenticated using(user_id=auth.uid());
create policy calendar_image_owner_guard on storage.objects as restrictive for all to authenticated
  using (bucket_id <> 'calendar_images' or (storage.foldername(name))[1]=auth.uid()::text)
  with check (bucket_id <> 'calendar_images' or (storage.foldername(name))[1]=auth.uid()::text);

-- SECURITY DEFINER functions must never be executable by browser roles.
revoke all on function public.register_location(text,uuid,text), public.change_tokens(text,uuid,numeric,text,text,text), public.settle_order(text,text,uuid), public.reserve_tokens(text,text,uuid,numeric), public.finish_tokens(text,boolean) from public, anon, authenticated;
grant execute on function public.register_location(text,uuid,text), public.change_tokens(text,uuid,numeric,text,text,text), public.settle_order(text,text,uuid), public.reserve_tokens(text,text,uuid,numeric), public.finish_tokens(text,boolean) to service_role;
grant all on public.location_profiles,public.account_token_ledger,public.billing_orders,public.token_operations,public.job_runs to service_role;
grant usage,select on sequence public.account_token_ledger_id_seq to service_role;
commit;
