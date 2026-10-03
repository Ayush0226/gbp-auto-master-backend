-- Add exact publication times for MCP while preserving date-based website scheduling.
begin;

alter table public.calendar_posts
  add column if not exists publish_at timestamptz;

-- Existing website rows were intended for the business day in Asia/Kolkata.
update public.calendar_posts
set publish_at = post_date::timestamp at time zone 'Asia/Kolkata'
where publish_at is null and post_date is not null;

create index if not exists calendar_posts_due_idx
  on public.calendar_posts(status, publish_at);

-- Keep the existing website RPC working. A date-only item becomes due at midnight
-- in the configured business timezone and is still visible through the MCP calendar.
create or replace function public.schedule_post(p_user uuid,p_location text,p_date date,p_caption text,p_image text,p_type text)
returns jsonb language plpgsql security definer set search_path = public as $$
declare v_result jsonb; v_id uuid := gen_random_uuid(); v_publish_at timestamptz;
begin
  if p_type not in ('LOCAL_POST','PHOTO','VIDEO') then raise exception 'Invalid post type'; end if;
  if not exists(select 1 from location_profiles where location_id=p_location and user_id=p_user) then
    raise exception 'Location ownership mismatch';
  end if;
  v_publish_at := p_date::timestamp at time zone 'Asia/Kolkata';
  v_result := change_tokens(p_location,p_user,-5,'schedule_post','Scheduled business post','schedule:'||v_id::text);
  if not (v_result->>'success')::boolean then raise exception 'Insufficient tokens'; end if;
  insert into calendar_posts(id,user_id,location_id,post_date,publish_at,caption,image_url,post_type,status)
    values(v_id,p_user,p_location,p_date,v_publish_at,p_caption,p_image,p_type,'scheduled');
  return jsonb_build_object('status','success','id',v_id,'balance',v_result->'balance','publish_at',v_publish_at);
end $$;

create or replace function public.schedule_post_at(p_user uuid,p_location text,p_publish_at timestamptz,p_caption text,p_image text,p_type text)
returns jsonb language plpgsql security definer set search_path = public as $$
declare v_result jsonb; v_id uuid := gen_random_uuid();
begin
  if p_publish_at is null or p_publish_at <= now() then raise exception 'Publication time must be in the future'; end if;
  if p_type not in ('LOCAL_POST','PHOTO','VIDEO') then raise exception 'Invalid post type'; end if;
  if coalesce(btrim(p_caption),'') = '' and p_image is null then raise exception 'Add text or media'; end if;
  if p_type in ('PHOTO','VIDEO') and p_image is null then raise exception 'Media URL required'; end if;
  if not exists(select 1 from location_profiles where location_id=p_location and user_id=p_user) then
    raise exception 'Location ownership mismatch';
  end if;
  v_result := change_tokens(p_location,p_user,-5,'schedule_post','Scheduled business post','schedule:'||v_id::text);
  if not (v_result->>'success')::boolean then raise exception 'Insufficient tokens'; end if;
  insert into calendar_posts(id,user_id,location_id,post_date,publish_at,caption,image_url,post_type,status)
    values(v_id,p_user,p_location,(p_publish_at at time zone 'Asia/Kolkata')::date,p_publish_at,p_caption,p_image,p_type,'scheduled');
  return jsonb_build_object('status','success','id',v_id,'balance',v_result->'balance','publish_at',p_publish_at);
end $$;

revoke all on function public.schedule_post(uuid,text,date,text,text,text) from public,anon,authenticated;
revoke all on function public.schedule_post_at(uuid,text,timestamptz,text,text,text) from public,anon,authenticated;
grant execute on function public.schedule_post(uuid,text,date,text,text,text) to service_role;
grant execute on function public.schedule_post_at(uuid,text,timestamptz,text,text,text) to service_role;

commit;
