"""Validate and protect existing grants without changing identity or access."""
import sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))

from dotenv import load_dotenv
load_dotenv(Path(__file__).resolve().parents[1]/'.env.local')
from main import supabase
from credential_store import google_refresh_grant


def migrate():
    users=supabase.auth.admin.list_users(page=1,per_page=100)
    connected=failed=0
    for user in users:
        if not (user.user_metadata or {}).get('google_refresh_token'):
            continue
        try:
            if google_refresh_grant(supabase,user): connected+=1
        except PermissionError:
            failed+=1
            print('A Google identity requires reconnection; no ownership or role was changed.')
    print({'grants_protected':connected,'reconnections_needed':failed})
    if failed: raise SystemExit(1)


if __name__=='__main__':migrate()
