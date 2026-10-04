from types import SimpleNamespace

from rank_service import run_local_rank_scan


def response(payload):
    return SimpleNamespace(ok=True, json=lambda: payload)


def test_rank_scan_returns_measured_position_and_only_top_11():
    calls = iter([
        response({
            'title': 'Ayush Cafe',
            'storefrontAddress': {'locality': 'Pune', 'administrativeArea': 'MH', 'regionCode': 'IN'},
            'metadata': {'placeId': 'target-place'},
        }),
        response({'local_results': [
            {'position': i, 'title': 'Ayush Cafe' if i == 7 else f'Cafe {i}', 'place_id': 'target-place' if i == 7 else f'p{i}'}
            for i in range(1, 14)
        ]}),
    ])
    report = run_local_rank_scan(
        location_id='locations/123', keyword='coffee', google_access_token='token',
        serpapi_key='serp', http_get=lambda *args, **kwargs: next(calls),
    )
    assert report['actual_rank'] == 7
    assert report['found_in_top_11'] is True
    assert len(report['results']) == 11


def test_rank_scan_does_not_invent_rank_when_target_is_absent():
    calls = iter([
        response({'title': 'Ayush Cafe', 'storefrontAddress': {'locality': 'Pune'}, 'metadata': {}}),
        response({'local_results': [{'position': i, 'title': f'Cafe {i}'} for i in range(1, 12)]}),
    ])
    report = run_local_rank_scan(
        location_id='locations/123', keyword='coffee', google_access_token='token',
        serpapi_key='serp', http_get=lambda *args, **kwargs: next(calls),
    )
    assert report['actual_rank'] is None
    assert report['found_in_top_11'] is False
