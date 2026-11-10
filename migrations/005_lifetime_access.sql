-- Run after migration 004. Payment checkout remains paused in the matching app.
begin;

alter table public.account_subscriptions
  add column if not exists is_lifetime boolean not null default false,
  add column if not exists access_scope text;
alter table public.account_subscriptions add constraint lifetime_subscription_shape
  check (not is_lifetime or (plan_type='yearly' and expires_at is null and access_scope is not distinct from 'current_features_v1'));
alter table public.user_profiles alter column max_seo_keywords drop not null;

create table public.access_codes (
  code_hash text primary key check (code_hash ~ '^[a-f0-9]{64}$'),
  enabled boolean not null default true,
  access_scope text not null check (access_scope='current_features_v1'),
  created_at timestamptz not null default now()
);
create table public.access_code_redemptions (
  user_id uuid not null references auth.users(id) on delete cascade,
  code_hash text not null references public.access_codes(code_hash),
  redeemed_at timestamptz not null default now(),
  primary key(user_id,code_hash)
);
alter table public.access_codes enable row level security;
alter table public.access_code_redemptions enable row level security;
revoke all on public.access_codes,public.access_code_redemptions from public,anon,authenticated;
grant all on public.access_codes,public.access_code_redemptions to service_role;

-- The launch code is stored only as SHA-256, not shipped to the browser.
insert into public.access_codes(code_hash,access_scope)
  values('95f5f58ce4ebb025e463a6b0df8adcfc4f59a231be52cdebe27b81f5b52a5364','current_features_v1')
on conflict(code_hash) do nothing;

-- Return effective account access instead of stale legacy user-profile plan fields.
create or replace function public.ensure_account(p_user uuid)
returns jsonb language plpgsql security definer set search_path=public as $$
declare v_profile user_profiles; v_sub account_subscriptions; v_plan text; v_unlimited boolean; v_limit integer;
begin
  insert into user_profiles(id,plan_type,tokens_balance,max_seo_keywords,seo_keywords,reply_length,demo_completed,onboarding_completed)
    values(p_user,'free',0,2,'{}','50-90',false,false) on conflict(id) do nothing;
  insert into account_subscriptions(user_id) values(p_user) on conflict(user_id) do nothing;
  select * into v_profile from user_profiles where id=p_user;
  select * into v_sub from account_subscriptions where user_id=p_user;
  v_plan := case when v_sub.status='active' and
    (v_sub.plan_type='free' or v_sub.is_lifetime or v_sub.expires_at>now())
    then v_sub.plan_type else 'free' end;
  v_unlimited := v_plan='yearly' and v_sub.is_lifetime and v_sub.access_scope='current_features_v1';
  v_limit := case when v_unlimited then null when v_plan='yearly' then 15
    when v_plan='half_yearly' then 10 when v_plan='monthly' then 5 else 2 end;
  return (to_jsonb(v_profile)-'tokens_balance'-'last_daily_claim') || jsonb_build_object(
    'plan_type',v_plan,'is_lifetime',v_unlimited,'unlimited_access',v_unlimited,
    'access_scope',case when v_unlimited then v_sub.access_scope else null end,
    'subscription_end',case when v_unlimited then null else v_sub.expires_at end,
    'max_seo_keywords',v_limit,'competitor_access',v_plan in ('half_yearly','yearly'));
end $$;

create or replace function public.redeem_lifetime_access(p_user uuid,p_code_hash text)
returns jsonb language plpgsql security definer set search_path=public as $$
declare v_code access_codes; v_sub account_subscriptions; v_previous boolean;
begin
  perform pg_advisory_xact_lock(hashtextextended(p_user::text,0));
  v_previous := exists(select 1 from access_code_redemptions where user_id=p_user and code_hash=p_code_hash);
  if v_previous then
    select * into v_sub from account_subscriptions where user_id=p_user;
    -- A retry must not restore an entitlement that an operator has revoked.
    return jsonb_build_object('success',v_sub.status='active' and v_sub.is_lifetime,
      'already_redeemed',true,'subscription',to_jsonb(v_sub)-'monthly_credit_allowance'-'next_credit_grant_at');
  end if;
  select * into v_code from access_codes where code_hash=p_code_hash and enabled for share;
  if not found then return jsonb_build_object('success',false); end if;
  perform ensure_account(p_user);
  update account_subscriptions set plan_type='yearly',status='active',expires_at=null,
    is_lifetime=true,access_scope=v_code.access_scope,max_locations=null,
    auto_renew=false,monthly_credit_allowance=0,next_credit_grant_at=null,updated_at=now()
    where user_id=p_user returning * into v_sub;
  update user_profiles set plan_type='yearly',max_seo_keywords=null,updated_at=now() where id=p_user;
  update location_profiles set plan_type='yearly',subscription_end=null,auto_renew=false,
    monthly_token_allowance=0,next_token_grant_at=null where user_id=p_user;
  insert into access_code_redemptions(user_id,code_hash) values(p_user,p_code_hash);
  insert into audit_events(user_id,actor_type,action,resource_type,resource_id,detail)
    values(p_user,'user','redeem_lifetime_access','account_subscription',p_user::text,
      jsonb_build_object('plan','yearly','is_lifetime',true,'access_scope',v_code.access_scope));
  return jsonb_build_object('success',true,'already_redeemed',false,
    'subscription',to_jsonb(v_sub)-'monthly_credit_allowance'-'next_credit_grant_at');
end $$;

revoke all on function public.redeem_lifetime_access(uuid,text) from public,anon,authenticated;
grant execute on function public.redeem_lifetime_access(uuid,text) to service_role;
-- A previous backend must not process paid orders during the checkout pause.
revoke all on function public.settle_order(text,text,uuid) from service_role;
notify pgrst, 'reload schema';
commit;
