"""Test-only: get a Dex ID token for the Claude Desktop client (#1414).

Runs inside the preloop container, which trusts the harness CA. Uses the
password grant against dex-idp so no browser is needed; Claude Desktop gets
the same ID token through its PKCE sign-in. Prints the token on stdout.
"""

import base64
import json
import ssl
import sys
import urllib.parse
import urllib.request

DEX = "https://dex-idp:5557/dex"


def main() -> int:
    user = sys.argv[1] if len(sys.argv) > 1 else "alice@example.com"
    context = ssl.create_default_context(cafile="/idp-tls/ca.pem")
    body = urllib.parse.urlencode(
        {
            "grant_type": "password",
            "username": user,
            "password": "password",
            "scope": "openid email profile",
        }
    ).encode()
    request = urllib.request.Request(f"{DEX}/token", data=body, method="POST")
    basic = base64.b64encode(b"claude-desktop:harness-desktop-client-secret").decode()
    request.add_header("Authorization", f"Basic {basic}")
    request.add_header("Content-Type", "application/x-www-form-urlencoded")
    with urllib.request.urlopen(request, context=context, timeout=10) as response:
        print(json.load(response)["id_token"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
