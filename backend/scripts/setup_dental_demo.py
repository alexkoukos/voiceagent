"""Create an isolated fictional dental demo through the authenticated API.

Run from backend with ADMIN_API_TOKEN set; existing practices are never updated.
"""
import argparse
import json
import os
import secrets
from pathlib import Path

import httpx


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--backend', default='http://localhost:8000')
    parser.add_argument('--permanent', action='store_true', help='Keep the link active until explicitly revoked')
    args = parser.parse_args()
    # Explicit destinations keep founder credentials away from arbitrary URLs.
    # Add a reviewed origin here when moving this repository to another host.
    origins = {
        'http://localhost:8000': 'http://localhost:8000',
        'http://127.0.0.1:8000': 'http://127.0.0.1:8000',
        'https://backend-production-c085.up.railway.app': 'https://backend-production-c085.up.railway.app',
    }
    base = origins.get(args.backend.rstrip('/'))
    if base is None:
        parser.error('Use a configured local or production backend origin.')
    token = os.environ.get('ADMIN_API_TOKEN')
    if not token:
        parser.error('Set ADMIN_API_TOKEN in the environment.')
    payload = json.loads((Path(__file__).resolve().parents[1] / 'config' / 'demo_dentist.json').read_text())
    payload['slug'] = 'dental-demo-' + secrets.token_hex(6)
    with httpx.Client(timeout=30) as client:
        response = client.post(base + '/practices', headers={'x-api-key': token}, json=payload)
    response.raise_for_status()
    practice = response.json()
    with httpx.Client(timeout=30) as client:
        shared = client.post(base + '/practices/' + practice['id'] + '/demo-dashboard',
                             headers={'x-api-key': token}, json={'expires_in_days': None if args.permanent else 7})
    shared.raise_for_status()
    dashboard = shared.json()
    print('Shareable live dashboard: ' + base + dashboard['path'])
    print('Dashboard expiry: ' + (dashboard['expires_at'] or 'No automatic expiry'))
    print('Dashboard ID (for revocation): ' + dashboard['id'])
    print('Demo created: ' + base + '/demo/' + practice['slug'])
    print('Public information: ' + base + '/demo/' + practice['slug'] + '/info')


if __name__ == '__main__':
    main()
