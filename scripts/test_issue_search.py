#!/usr/bin/env python3
import json

import requests

# Base URL for the Preloop API
base_url = "http://localhost:8000/api/v1"

# Authenticate and get a token
print("Authenticating...")
auth_response = requests.post(
    f"{base_url}/auth/token", data={"username": "admin", "password": "admin"}
)

if auth_response.status_code != 200:
    print(f"Authentication failed: {auth_response.status_code}")
    print(auth_response.text)
    raise SystemExit(1)

token_data = auth_response.json()
access_token = token_data.get("access_token")
headers = {"Authorization": f"Bearer {access_token}"}

print(f"Successfully authenticated, token: {access_token[:10]}...")

# Test searching for issues
print("\nSearching for issues...")
try:
    response = requests.get(
        f"{base_url}/issues/search",
        headers=headers,
        params={
            "organization": "spacecode",
            "project": "astrobot",
            "query": "authentication",
            "limit": 5,
        },
    )
    print(f"Status code: {response.status_code}")
    print(
        f"Response: {json.dumps(response.json(), indent=2) if response.status_code == 200 else response.text}"
    )
except Exception as e:
    print(f"Error: {e}")
