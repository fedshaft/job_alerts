#!/usr/bin/env python3
"""One-time: get a Gmail refresh token for gmail_alerts.py (read-only scope).

    python3 gmail_setup.py path/to/client_secret.json

Opens Google's consent page in your browser; after you allow access, the
browser is sent back to a tiny server on 127.0.0.1 run by this script,
which swaps the code for a refresh token and prints the three values to
store as GitHub secrets. Nothing is written to disk.
"""

import base64
import hashlib
import http.server
import json
import os
import secrets
import sys
import urllib.parse
import urllib.request
import webbrowser

SCOPE = "https://www.googleapis.com/auth/gmail.readonly"


def main():
    if len(sys.argv) != 2:
        sys.exit(__doc__)
    conf = json.load(open(sys.argv[1]))
    conf = conf.get("installed") or conf.get("web")
    client_id, client_secret = conf["client_id"], conf["client_secret"]

    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    state = secrets.token_urlsafe(16)
    result = {}

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            result.update({k: v[0] for k, v in q.items()})
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"Done. You can close this tab and go back to the terminal.")

        def log_message(self, *args):
            pass

    server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    redirect = f"http://127.0.0.1:{server.server_port}/"
    url = "https://accounts.google.com/o/oauth2/v2/auth?" + urllib.parse.urlencode({
        "client_id": client_id, "redirect_uri": redirect, "response_type": "code",
        "scope": SCOPE, "access_type": "offline", "prompt": "consent",
        "code_challenge": challenge, "code_challenge_method": "S256", "state": state,
    })
    print("Opening your browser. If it doesn't open, visit:\n" + url)
    webbrowser.open(url)
    while "code" not in result and "error" not in result:
        server.handle_request()
    if result.get("error") or result.get("state") != state:
        sys.exit(f"Not authorised: {result.get('error', 'state mismatch')}")

    body = urllib.parse.urlencode({
        "code": result["code"], "client_id": client_id, "client_secret": client_secret,
        "redirect_uri": redirect, "grant_type": "authorization_code", "code_verifier": verifier,
    }).encode()
    with urllib.request.urlopen("https://oauth2.googleapis.com/token", body, timeout=20) as r:
        token = json.load(r)
    if "refresh_token" not in token:
        sys.exit("Google returned no refresh token; remove the app's access at "
                 "myaccount.google.com/permissions and run this again.")
    print("\nStore these three as GitHub secrets (gh secret set NAME), and export them to test locally:")
    print(f"GMAIL_CLIENT_ID={client_id}")
    print(f"GMAIL_CLIENT_SECRET={client_secret}")
    print(f"GMAIL_REFRESH_TOKEN={token['refresh_token']}")


if __name__ == "__main__":
    main()
