-- Cover referencing columns without changing policies, grants, roles or rows.
-- Small existing tables were inspected before application. A short lock timeout
-- aborts rather than blocking production if an unexpected long transaction runs.
begin;
set local lock_timeout='3s';
do $$
declare r record; cols text;
begin
  for r in
    select c.conrelid,c.conkey,c.conname,t.relname
    from pg_constraint c join pg_class t on t.oid=c.conrelid
      join pg_namespace n on n.oid=t.relnamespace
    where c.contype='f' and n.nspname='public' and not exists (
      select 1 from pg_index i where i.indrelid=c.conrelid and i.indisvalid and i.indpred is null
      and (select array_agg(k order by ord) from unnest(i.indkey) with ordinality as x(k,ord)
        where ord<=array_length(c.conkey,1))=c.conkey
    )
  loop
    select string_agg(quote_ident(a.attname),',' order by k.ord) into cols
      from unnest(r.conkey) with ordinality k(attnum,ord)
      join pg_attribute a on a.attrelid=r.conrelid and a.attnum=k.attnum;
    execute format('create index if not exists %I on public.%I (%s)',
      left(r.conname,49)||'_cover_idx',r.relname,cols);
  end loop;
end $$;
commit;
