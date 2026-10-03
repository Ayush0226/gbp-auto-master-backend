"""Bound external request durations, including calls in legacy endpoints."""
import requests as _requests


class Client:
    def __getattr__(self, method):
        if method not in {'get', 'post', 'put', 'patch', 'delete'}:
            return getattr(_requests, method)
        def call(url, **kwargs):
            kwargs.setdefault('timeout', (5, 30))
            return _requests.request(method, url, **kwargs)
        return call


requests = Client()
