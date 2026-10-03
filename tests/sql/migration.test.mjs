import { PGlite } from '@electric-sql/pglite';
import { readFile } from 'node:fs/promises';
import assert from 'node:assert/strict';

const db = new PGlite();
const owner = '11111111-1111-4111-8111-111111111111';
const other = '22222222-2222-4222-8222-222222222222';
const fresh = '33333333-3333-4333-8333-333333333333';
await db.exec(`
create role anon; create role authenticated; create role service_role bypassrls;
create schema auth; create schema storage;
create function auth.uid() returns uuid language sql as $$ select nullif(current_setting('request.jwt.claim.sub',true),'')::uuid $$;
create table auth.users(id uuid primary key,email text,email_confirmed_at timestamptz,raw_app_meta_data jsonb,raw_user_meta_data jsonb);
create table public.user_profiles(id uuid primary key references auth.users(id), plan_type text default 'free',tokens_balance numeric default 0,max_seo_keywords integer,seo_keywords text[],reply_length text,demo_completed boolean,onboarding_completed boolean,updated_at timestamptz default now());
create table public.token_ledger(id bigint primary key,user_id uuid,amount numeric,action text,description text,reference_id text,created_at timestamptz default now());
create table public.user_settings(user_id uuid);
create table public.locations(id uuid,user_id uuid);
create table public.scheduled_posts(id uuid,user_id uuid);
create table public.calendar_posts(id uuid primary key,user_id uuid,location_id text,post_date date,caption text,image_url text,post_type text,status text);
create table storage.objects(id uuid,bucket_id text,name text);
create function storage.foldername(name text) returns text[] language sql as $$ select string_to_array(name,'/') $$;
grant usage on schema public,auth,storage to authenticated,service_role;
grant all on all tables in schema public to authenticated,service_role;
insert into auth.users values
 ('${owner}','ayushsony126@gmail.com',now(),'{}','{"subscriptions":{"locations/1":{"status":"active","plan_id":"yearly","expires_at":"2030-01-01T00:00:00Z"}}}'),
 ('${other}','other@example.test',now(),'{}','{}'),('${fresh}','fresh@example.test',now(),'{}','{}');
insert into user_profiles(id,tokens_balance) values('${owner}',60),('${other}',25);
insert into token_ledger(id,user_id,amount,action) values(1,'${owner}',60,'onboarding_bonus');
`);

await db.exec(await readFile(new URL('../../migrations/001_secure_billing.sql', import.meta.url), 'utf8'));
await db.exec(await readFile(new URL('../../migrations/002_mcp_scheduling.sql', import.meta.url), 'utf8'));
let assertions = 0;
async function scalar(sql, params=[]) { return Object.values((await db.query(sql, params)).rows[0])[0]; }
async function balance(user=owner) { return Number(await scalar('select tokens_balance from user_profiles where id=$1',[user])); }
function equal(a,b,message) { assert.deepEqual(a,b,message); assertions++; }
async function rejects(sql,params=[]) { await assert.rejects(db.query(sql,params)); assertions++; }
equal(await balance(),60,'migration must preserve the current account balance');
equal(Number(await scalar('select count(*) from account_token_ledger')),1,'legacy history copied once');
equal(await scalar('select raw_app_meta_data->>\'role\' from auth.users where id=$1',[owner]),'admin');
await db.query('select ensure_account($1)',[fresh]);
await db.query('select ensure_account($1)',[fresh]);
equal(await balance(fresh),200,'signup bonus once per account');
await db.query('select register_location($1,$2,$3)',['locations/1',owner,'accounts/10']);
await db.query('select register_location($1,$2,$3)',['locations/2',owner,'accounts/10']);
await db.query('select register_location($1,$2,$3)',['locations/3',other,'accounts/20']);
equal(await balance(),60,'connecting profiles must not mint more tokens');
equal(await scalar('select plan_type from location_profiles where location_id=$1',['locations/1']),'yearly','legacy plan carried forward');
await rejects('select register_location($1,$2,$3)',['locations/1',other,'accounts/20']);
await db.query('select change_tokens($1,$2,$3,$4,$5,$6)',['locations/1',owner,-10,'test','first profile','spend-1']);
await db.query('select change_tokens($1,$2,$3,$4,$5,$6)',['locations/2',owner,-5,'test','second profile','spend-2']);
equal(await balance(),45,'both profiles spend the same balance');
equal(await balance(other),25,'other accounts remain isolated');
await rejects('select change_tokens($1,$2,$3,$4,$5,$6)',['locations/3',owner,-5,'test','wrong owner','spend-wrong']);
equal(await balance(),45);
const results=await Promise.all([1,2].map(i=>db.query('select change_tokens($1,$2,$3,$4,$5,$6)',['locations/1',owner,-30,'test','competing debit','race-'+i])));
equal(results.filter(r=>r.rows[0].change_tokens.success).length,1,'only affordable spending succeeds');
equal(await balance(),15);
await db.query(`insert into billing_orders(order_id,user_id,location_id,kind,product_id,amount,tokens,duration_months) values('order-1',$1,'locations/1','subscription','yearly',500000,750,12)`,[owner]);
await db.query('select settle_order($1,$2,$3)',['order-1','payment-1',owner]);
await db.query('select settle_order($1,$2,$3)',['order-1','payment-1',owner]);
equal(await balance(),765,'payment replay must not grant credits twice');
await db.query(`insert into billing_orders(order_id,user_id,location_id,kind,product_id,amount,tokens) values('topup-1',$1,null,'topup','standard',50000,450)`,[owner]);
await rejects('select settle_order($1,$2,$3)',['topup-1','payment-1',owner]);
equal(await balance(),765,'payment reuse must roll back');
await db.query('select settle_order($1,$2,$3)',['topup-1','payment-2',owner]);
equal(await balance(),1215,'top-up credits the account without a selected profile');
await db.query('select reserve_tokens($1,$2,$3,$4)',['reply-1','locations/1',owner,2.5]);
equal(await balance(),1212.5);
equal((await scalar('select reserve_tokens($1,$2,$3,$4)',['reply-1','locations/1',owner,2.5])).success,false);
await db.query('select finish_tokens($1,$2)',['reply-1',false]);
await db.query('select finish_tokens($1,$2)',['reply-1',false]);
equal(await balance(),1215,'refund is idempotent');
await db.query(`update location_profiles set next_token_grant_at=now()-interval '1 day' where location_id='locations/1'`);
await db.query('select refresh_monthly_tokens($1,$2)',['locations/1',owner]);
await db.query('select refresh_monthly_tokens($1,$2)',['locations/1',owner]);
equal(await balance(),1965,'monthly grant runs once and credits the shared account');
await db.query('select schedule_post($1,$2,$3,$4,$5,$6)',[owner,'locations/2','2026-10-04','Hello',null,'LOCAL_POST']);
equal(await balance(),1960);
equal(Number(await scalar('select count(*) from calendar_posts')),1);
await db.query(`select schedule_post_at($1,$2,now()+interval '1 day',$3,$4,$5)`,[owner,'locations/2','Timed post',null,'LOCAL_POST']);
equal(await balance(),1955,'exact-time MCP scheduling charges once');
equal(Number(await scalar('select count(*) from calendar_posts where publish_at is not null')),2,'all scheduled posts have a publication timestamp');
equal(await scalar('select claim_job($1)',['cron']),true);
equal(await scalar('select claim_job($1)',['cron']),false);
await db.exec(`set role authenticated; set request.jwt.claim.sub='${other}';`);
equal(Number(await scalar('select count(*) from account_token_ledger')),0,'RLS hides another account history');
await rejects(`update user_profiles set tokens_balance=999999 where id='${other}'`);
await rejects('select change_tokens($1,$2,$3,$4,$5,$6)',[null,other,100,'forge','forge',null]);
await rejects(`insert into calendar_posts(id,user_id,location_id) values(gen_random_uuid(),'${other}','locations/3')`);
await db.exec('reset role');
console.log(`${assertions} database assertions passed against PostgreSQL (PGlite).`);
await db.close();
