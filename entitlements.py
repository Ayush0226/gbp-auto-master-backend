"""Normalize server-owned account access for the website, workers and MCP."""
from datetime import datetime, timezone


PLAN_KEYWORD_LIMITS = {'free': 2, 'monthly': 5, 'half_yearly': 10, 'yearly': 15}
CURRENT_FEATURE_SCOPE = 'current_features_v1'


def resolve_subscription(row=None, *, now=None):
    row = dict(row or {})
    now = now or datetime.now(timezone.utc)
    plan = row.get('plan_type') or 'free'
    status = row.get('status') or 'active'
    expiry = row.get('expires_at')
    lifetime = bool(row.get('is_lifetime')) and plan == 'yearly'
    active = status == 'active'
    if not lifetime and plan != 'free':
        try:
            end = datetime.fromisoformat(str(expiry).replace('Z', '+00:00'))
            active = active and end.tzinfo is not None and end > now
        except (ValueError, TypeError):
            active = False
    if plan not in PLAN_KEYWORD_LIMITS:
        active = False
    if not active:
        if status == 'active':
            status = 'expired'
        plan = 'free'
        lifetime = False
    unlimited = lifetime and row.get('access_scope') == CURRENT_FEATURE_SCOPE
    return {
        **row,
        'plan_type': plan,
        'status': status,
        'expires_at': None if lifetime else expiry,
        'is_lifetime': lifetime,
        'unlimited_access': unlimited,
        'access_scope': row.get('access_scope') if unlimited else None,
        'max_seo_keywords': None if unlimited else PLAN_KEYWORD_LIMITS[plan],
        'max_locations': None if unlimited else row.get('max_locations'),
        'competitor_access': plan in {'half_yearly', 'yearly'},
    }
