"""Verify existing Google connections after migration. --apply enables writes."""
import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import main
from credential_store import read_credential


async def run(apply):
    users = main.list_all_users()
    protected = {row['user_id'] for row in main.supabase.table('provider_credentials').select('user_id').eq('provider','google').execute().data or []}
    connected = [u for u in users if u.id in protected or (u.user_metadata or {}).get('google_refresh_token')]
    print(f'{len(connected)} accounts have a saved Google connection.')
    if not apply:
        print('Dry run: no locations registered and no balances changed.')
        return
    succeeded = failed = 0
    for user in connected:
        try:
            grant = read_credential(main.supabase,user.id,'google') or user.user_metadata['google_refresh_token']
            token = main.get_offline_access_token(grant)
            await main.get_google_locations(main.GoogleSyncRequest(user_id=user.id, provider_token=token))
            succeeded += 1
        except Exception:
            # Do not print tokens or raw provider responses.
            failed += 1
    print(f'{succeeded} accounts registered; {failed} need a Google reconnection.')
    if failed:
        raise SystemExit(1)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    asyncio.run(run(args.apply))
