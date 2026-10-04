-- Account-first foundation for subscriptions, automation, campaigns and media.
-- Additive migration: v1 location and calendar workflows remain available.
begin;

create table if not exists public.account_subscriptions (
  user_id uuid primary key references auth.users(id) on delete cascade,
  plan_type text not null default 'free',
  status text not null default 'active' check (status in ('active','expired','cancelled','past_due')),
  starts_at timestamptz not null default now(),
  expires_at timestamptz,
  monthly_credit_allowance numeric not null default 0 check (monthly_credit_allowance >= 0),
  next_credit_grant_at timestamptz,
  auto_renew boolean not null default false,
  max_locations integer,
  updated_at timestamptz not null default now()
);

insert into public.account_subscriptions(
  user_id, plan_type, status, expires_at, monthly_credit_allowance,
  next_credit_grant_at, auto_renew
)
select distinct on (user_id)
  user_id,
  plan_type,
  case when subscription_end is null or subscription_end > now() then 'active' else 'expired' end,
  subscription_end,
  monthly_token_allowance,
  next_token_grant_at,
  auto_renew
from public.location_profiles
order by user_id,
  case plan_type when 'yearly' then 4 when 'half_yearly' then 3 when 'monthly' then 2 else 1 end desc,
  subscription_end desc nulls last
on conflict(user_id) do nothing;

insert into public.account_subscriptions(user_id)
select id from auth.users
on conflict(user_id) do nothing;

create table if not exists public.google_connections (
  id uuid primary key default gen_random_uuid(),
  user_id uuid not null references auth.users(id) on delete cascade,
  google_subject text,
  email text,
  status text not null default 'connected' check (status in ('connected','needs_reauth','revoked')),
  scopes text[] not null default '{}',
  token_secret_ref text,
  connected_at timestamptz not null default now(),
  last_synced_at timestamptz,
  unique(user_id, google_subject)
);

create table if not exists public.gbp_accounts (
  resource_name text primary key check (resource_name ~ '^accounts/[A-Za-z0-9_-]+$'),
  user_id uuid not null references auth.users(id) on delete cascade,
  connection_id uuid references public.google_connections(id) on delete set null,
  display_name text not null default 'Google Business Profile account',
  account_type text,
  role text,
  verification_state text,
  last_synced_at timestamptz,
  updated_at timestamptz not null default now()
);

insert into public.gbp_accounts(resource_name,user_id,last_synced_at)
select distinct account_id,user_id,now()
from public.location_profiles
where account_id ~ '^accounts/[A-Za-z0-9_-]+$'
on conflict(resource_name) do update
set user_id=excluded.user_id,last_synced_at=excluded.last_synced_at,updated_at=now();

create or replace function public.sync_location_gbp_account()
returns trigger language plpgsql security definer set search_path=public as $$
begin
  insert into gbp_accounts(resource_name,user_id,last_synced_at)
    values(new.account_id,new.user_id,now())
  on conflict(resource_name) do update
    set user_id=excluded.user_id,last_synced_at=excluded.last_synced_at,updated_at=now();
  return new;
end $$;
drop trigger if exists sync_location_gbp_account_trigger on public.location_profiles;
create trigger sync_location_gbp_account_trigger
after insert or update of account_id,user_id on public.location_profiles
for each row execute function public.sync_location_gbp_account();

create table if not exists public.account_preferences (
  user_id uuid primary key references auth.users(id) on delete cascade,
  timezone text not null default 'Asia/Kolkata',
  default_account_id text references public.gbp_accounts(resource_name) on delete set null,
  default_location_id text references public.location_profiles(location_id) on delete set null,
  locale text not null default 'en-IN',
  updated_at timestamptz not null default now()
);

create table if not exists public.review_automation_rules (
  id uuid primary key default gen_random_uuid(),
  user_id uuid not null references auth.users(id) on delete cascade,
  location_id text references public.location_profiles(location_id) on delete cascade,
  name text not null,
  enabled boolean not null default false,
  mode text not null default 'draft' check (mode in ('draft','approval','auto_publish')),
  min_rating integer not null default 4 check (min_rating between 1 and 5),
  max_rating integer not null default 5 check (max_rating between 1 and 5),
  tone text not null default 'friendly professional',
  language text not null default 'auto',
  custom_instructions text not null default '',
  delay_minutes integer not null default 0 check (delay_minutes between 0 and 10080),
  daily_limit integer not null default 20 check (daily_limit between 1 and 500),
  blocked_terms text[] not null default '{}',
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  check (min_rating <= max_rating)
);
create unique index if not exists review_rule_account_default_idx
  on public.review_automation_rules(user_id) where location_id is null;
create unique index if not exists review_rule_location_idx
  on public.review_automation_rules(user_id,location_id) where location_id is not null;

create table if not exists public.media_assets (
  id uuid primary key default gen_random_uuid(),
  user_id uuid not null references auth.users(id) on delete cascade,
  storage_bucket text not null default 'calendar_images',
  object_path text not null,
  public_url text,
  media_kind text not null check (media_kind in ('photo','video')),
  mime_type text not null,
  byte_size bigint check (byte_size is null or byte_size >= 0),
  width integer,
  height integer,
  duration_seconds numeric,
  status text not null default 'uploaded' check (status in ('uploading','uploaded','validated','rejected','deleted')),
  validation_error text,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  unique(user_id,storage_bucket,object_path)
);

create table if not exists public.content_campaigns (
  id uuid primary key default gen_random_uuid(),
  user_id uuid not null references auth.users(id) on delete cascade,
  title text not null,
  topic_type text not null default 'STANDARD' check (topic_type in ('STANDARD','EVENT','OFFER')),
  summary text not null default '',
  language_code text not null default 'en',
  call_to_action jsonb,
  event_details jsonb,
  offer_details jsonb,
  media_asset_id uuid references public.media_assets(id) on delete set null,
  timezone text not null default 'Asia/Kolkata',
  status text not null default 'draft' check (status in ('draft','awaiting_approval','scheduled','processing','partially_published','published','failed','cancelled')),
  scheduled_for timestamptz,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

create table if not exists public.campaign_locations (
  campaign_id uuid not null references public.content_campaigns(id) on delete cascade,
  location_id text not null references public.location_profiles(location_id) on delete cascade,
  user_id uuid not null references auth.users(id) on delete cascade,
  caption_override text,
  media_asset_id uuid references public.media_assets(id) on delete set null,
  scheduled_for timestamptz,
  status text not null default 'draft' check (status in ('draft','scheduled','publishing','published','failed','cancelled')),
  google_post_name text,
  google_search_url text,
  attempt_count integer not null default 0,
  last_error text,
  published_at timestamptz,
  updated_at timestamptz not null default now(),
  primary key(campaign_id,location_id)
);
create index if not exists campaign_locations_due_idx
  on public.campaign_locations(status,scheduled_for);

create table if not exists public.inbound_events (
  event_id text primary key,
  user_id uuid references auth.users(id) on delete cascade,
  location_id text references public.location_profiles(location_id) on delete cascade,
  event_type text not null,
  payload jsonb not null default '{}',
  status text not null default 'received' check (status in ('received','processing','completed','failed','ignored')),
  attempt_count integer not null default 0,
  last_error text,
  received_at timestamptz not null default now(),
  processed_at timestamptz
);

create table if not exists public.review_reply_jobs (
  id uuid primary key default gen_random_uuid(),
  event_id text references public.inbound_events(event_id) on delete set null,
  user_id uuid not null references auth.users(id) on delete cascade,
  location_id text not null references public.location_profiles(location_id) on delete cascade,
  rule_id uuid references public.review_automation_rules(id) on delete set null,
  review_name text not null,
  rating integer not null check (rating between 1 and 5),
  review_text text not null default '',
  draft_text text not null,
  status text not null check (status in ('draft','pending_approval','scheduled','publishing','published','failed','cancelled')),
  scheduled_for timestamptz,
  published_at timestamptz,
  attempt_count integer not null default 0,
  last_error text,
  credits_charged numeric not null default 0,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  unique(user_id,review_name)
);
create index if not exists review_reply_jobs_due_idx
  on public.review_reply_jobs(status,scheduled_for);

create table if not exists public.audit_events (
  id bigint generated always as identity primary key,
  user_id uuid not null references auth.users(id) on delete cascade,
  location_id text references public.location_profiles(location_id) on delete set null,
  actor_type text not null check (actor_type in ('user','agent','automation','system')),
  action text not null,
  resource_type text not null,
  resource_id text,
  detail jsonb not null default '{}',
  created_at timestamptz not null default now()
);
create index if not exists audit_events_owner_time_idx
  on public.audit_events(user_id,created_at desc);

create or replace function public.claim_inbound_event(
  p_event text,p_user uuid,p_location text,p_type text,p_payload jsonb
)
returns boolean language plpgsql security definer set search_path=public as $$
begin
  insert into inbound_events(event_id,user_id,location_id,event_type,payload,status,attempt_count)
    values(p_event,p_user,p_location,p_type,coalesce(p_payload,'{}'::jsonb),'processing',1)
  on conflict(event_id) do update set
    status='processing',attempt_count=inbound_events.attempt_count+1,
    payload=excluded.payload,last_error=null,received_at=now(),processed_at=null
  where inbound_events.status='failed'
     or (inbound_events.status='processing' and inbound_events.received_at<now()-interval '15 minutes');
  return found;
end $$;

-- Subscription purchases now update the account entitlement. The legacy location
-- fields are updated as a compatibility mirror until all v1 clients are retired.
create or replace function public.settle_order(p_order text,p_payment text,p_user uuid)
returns jsonb language plpgsql security definer set search_path=public as $$
declare v_order billing_orders; v_result jsonb; v_expires timestamptz;
begin
  select * into v_order from billing_orders where order_id=p_order and user_id=p_user for update;
  if not found then raise exception 'Unknown order'; end if;
  if v_order.processed_at is not null then return jsonb_build_object('status','success','already_processed',true); end if;
  if exists(select 1 from billing_orders where payment_id=p_payment) then raise exception 'Payment already used'; end if;
  v_result := change_tokens(v_order.location_id,p_user,v_order.tokens,v_order.kind||'_purchase','Order '||p_order,'order:'||p_order);
  if v_order.kind='subscription' then
    v_expires := now()+make_interval(months=>v_order.duration_months);
    insert into account_subscriptions(user_id,plan_type,status,starts_at,expires_at,monthly_credit_allowance,next_credit_grant_at,auto_renew,updated_at)
      values(p_user,v_order.product_id,'active',now(),v_expires,v_order.tokens,now()+interval '1 month',false,now())
    on conflict(user_id) do update set
      plan_type=excluded.plan_type,status='active',starts_at=excluded.starts_at,
      expires_at=excluded.expires_at,monthly_credit_allowance=excluded.monthly_credit_allowance,
      next_credit_grant_at=excluded.next_credit_grant_at,auto_renew=false,updated_at=now();
    update location_profiles set plan_type=v_order.product_id,subscription_end=v_expires,
      auto_renew=false,monthly_token_allowance=v_order.tokens,next_token_grant_at=now()+interval '1 month'
      where user_id=p_user;
  end if;
  update billing_orders set payment_id=p_payment,processed_at=now() where order_id=p_order;
  return v_result || jsonb_build_object('status','success','tokens_added',v_order.tokens);
end $$;

-- Calls from multiple location views all converge on the same account grant date,
-- preventing one subscription from granting credits once per location.
create or replace function public.refresh_monthly_tokens(p_location text,p_user uuid)
returns void language plpgsql security definer set search_path=public as $$
declare v_subscription account_subscriptions; v_due timestamptz;
begin
  if p_location is not null and not exists(select 1 from location_profiles where location_id=p_location and user_id=p_user) then
    raise exception 'Location ownership mismatch';
  end if;
  perform 1 from user_profiles where id=p_user for update;
  select * into v_subscription from account_subscriptions where user_id=p_user for update;
  if not found then
    insert into account_subscriptions(user_id) values(p_user) returning * into v_subscription;
  end if;
  v_due := v_subscription.next_credit_grant_at;
  while v_due is not null and v_due<=now() and v_due<v_subscription.expires_at and v_subscription.monthly_credit_allowance>0 loop
    perform change_tokens(null,p_user,v_subscription.monthly_credit_allowance,'monthly_grant','Monthly account plan credits','monthly-account:'||p_user::text||':'||v_due::text);
    v_due := v_due+interval '1 month';
  end loop;
  update account_subscriptions set next_credit_grant_at=v_due,
    status=case when expires_at is not null and expires_at<=now() then 'expired' else status end,
    updated_at=now() where user_id=p_user;
end $$;

create or replace function public.create_content_campaign(
  p_user uuid,p_title text,p_topic_type text,p_summary text,p_timezone text,
  p_location_ids text[],p_call_to_action jsonb default null,
  p_event_details jsonb default null,p_offer_details jsonb default null,
  p_media_asset uuid default null
)
returns jsonb language plpgsql security definer set search_path=public as $$
declare v_campaign uuid := gen_random_uuid(); v_location text;
begin
  if coalesce(btrim(p_title),'')='' then raise exception 'Campaign title is required'; end if;
  if p_topic_type not in ('STANDARD','EVENT','OFFER') then raise exception 'Unsupported post type'; end if;
  if coalesce(array_length(p_location_ids,1),0)=0 then raise exception 'Select at least one location'; end if;
  if p_topic_type in ('EVENT','OFFER') and p_event_details is null then raise exception 'Event dates are required'; end if;
  if p_topic_type='OFFER' and p_offer_details is null then raise exception 'Offer details are required'; end if;
  if p_media_asset is not null and not exists(select 1 from media_assets where id=p_media_asset and user_id=p_user and status in ('uploaded','validated')) then raise exception 'Media asset is unavailable'; end if;
  foreach v_location in array p_location_ids loop
    if not exists(select 1 from location_profiles where location_id=v_location and user_id=p_user) then raise exception 'Location ownership mismatch'; end if;
  end loop;
  insert into content_campaigns(id,user_id,title,topic_type,summary,timezone,call_to_action,event_details,offer_details,media_asset_id)
    values(v_campaign,p_user,btrim(p_title),p_topic_type,coalesce(p_summary,''),p_timezone,p_call_to_action,p_event_details,p_offer_details,p_media_asset);
  insert into campaign_locations(campaign_id,location_id,user_id,media_asset_id)
    select v_campaign,location_id,p_user,p_media_asset from unnest(p_location_ids) location_id;
  insert into audit_events(user_id,actor_type,action,resource_type,resource_id,detail)
    values(p_user,'user','create','content_campaign',v_campaign::text,jsonb_build_object('locations',array_length(p_location_ids,1)));
  return jsonb_build_object('status','draft','campaign_id',v_campaign,'location_count',array_length(p_location_ids,1));
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
  v_cost := v_count*5;
  v_result := change_tokens(null,p_user,-v_cost,'schedule_campaign','Scheduled content campaign','campaign:'||p_campaign::text);
  if not (v_result->>'success')::boolean then raise exception 'Insufficient tokens'; end if;
  update content_campaigns set status='scheduled',scheduled_for=p_publish_at,updated_at=now() where id=p_campaign;
  update campaign_locations set status='scheduled',scheduled_for=p_publish_at,updated_at=now() where campaign_id=p_campaign;
  insert into audit_events(user_id,actor_type,action,resource_type,resource_id,detail)
    values(p_user,'user','schedule','content_campaign',p_campaign::text,jsonb_build_object('locations',v_count,'credits',v_cost,'publish_at',p_publish_at));
  return jsonb_build_object('status','scheduled','campaign_id',p_campaign,'location_count',v_count,'credits_charged',v_cost,'balance',v_result->'balance','publish_at',p_publish_at);
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
    values(p_user,'user','cancel','content_campaign',p_campaign::text,'{"credits_refunded":0}'::jsonb);
  return jsonb_build_object('status','cancelled','campaign_id',p_campaign,'credits_refunded',0);
end $$;

alter table public.account_subscriptions enable row level security;
alter table public.google_connections enable row level security;
alter table public.gbp_accounts enable row level security;
alter table public.account_preferences enable row level security;
alter table public.review_automation_rules enable row level security;
alter table public.media_assets enable row level security;
alter table public.content_campaigns enable row level security;
alter table public.campaign_locations enable row level security;
alter table public.inbound_events enable row level security;
alter table public.review_reply_jobs enable row level security;
alter table public.audit_events enable row level security;

do $$ declare table_name text; begin
  foreach table_name in array array['account_subscriptions','google_connections','gbp_accounts','account_preferences','review_automation_rules','media_assets','content_campaigns','campaign_locations','inbound_events','review_reply_jobs','audit_events'] loop
    execute format('revoke all on public.%I from anon,authenticated',table_name);
    execute format('grant select on public.%I to authenticated',table_name);
    execute format('grant all on public.%I to service_role',table_name);
    execute format('create policy %I on public.%I for select to authenticated using (user_id=auth.uid())',table_name||'_owner_read',table_name);
  end loop;
end $$;
grant usage,select on sequence public.audit_events_id_seq to service_role;

revoke all on function public.create_content_campaign(uuid,text,text,text,text,text[],jsonb,jsonb,jsonb,uuid), public.schedule_content_campaign(uuid,uuid,timestamptz), public.cancel_content_campaign(uuid,uuid), public.claim_inbound_event(text,uuid,text,text,jsonb), public.sync_location_gbp_account(), public.settle_order(text,text,uuid), public.refresh_monthly_tokens(text,uuid) from public,anon,authenticated;
grant execute on function public.create_content_campaign(uuid,text,text,text,text,text[],jsonb,jsonb,jsonb,uuid), public.schedule_content_campaign(uuid,uuid,timestamptz), public.cancel_content_campaign(uuid,uuid), public.claim_inbound_event(text,uuid,text,text,jsonb), public.settle_order(text,text,uuid), public.refresh_monthly_tokens(text,uuid) to service_role;

commit;
