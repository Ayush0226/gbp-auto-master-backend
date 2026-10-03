"""Verify existing Google connections after migration. --apply enables writes."""
import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import main


async def run(apply):
    users = main.list_all_users()
    connected = [u for u in users if (u.user_metadata or {}).get('google_refresh_token')]
    print(f'{len(connected)} accounts have a saved Google connection.')
    if not apply:
        print('Dry run: no locations registered and no balances changed.')
        return
    succeeded = failed = 0
    for user in connected:
        try:
            token = main.get_offline_access_token(user.user_metadata['google_refresh_token'])
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
