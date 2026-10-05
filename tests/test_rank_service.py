from types import SimpleNamespace

import time

from rank_service import create_rank_pdf_token, read_rank_pdf_token, render_rank_report_pdf, run_local_rank_scan


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


def test_rank_report_pdf_token_round_trip_and_pdf_render():
    report = {
        'keyword': 'coffee shop', 'target_business': 'Ayush Cafe',
        'search_area': 'Pune, MH, IN', 'actual_rank': 2, 'found_in_top_11': True,
        'results': [{'position': 2, 'business_name': 'Ayush Cafe', 'rating': 4.8,
                     'reviews': 120, 'address': 'Main Road', 'place_id': 'p1', 'is_target': True}],
    }
    token, expires_at = create_rank_pdf_token(report, 'test-secret', 60)
    assert expires_at > time.time()
    decoded = read_rank_pdf_token(token, 'test-secret')
    assert decoded['actual_rank'] == 2
    pdf = render_rank_report_pdf(decoded)
    assert pdf.startswith(b'%PDF-1.4')
    assert b'Ayush Cafe' in pdf
