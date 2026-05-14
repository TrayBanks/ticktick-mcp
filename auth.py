"""
One-time OAuth setup for TickTick MCP.

Run this script once to authorize Claude to access your TickTick account.
It will save tokens to .tokens.json in this directory.

Usage:
    python auth.py
"""

import json
import os
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlparse

import httpx
from dotenv import load_dotenv

load_dotenv()

REDIRECT_URI = "http://localhost:8080/callback"
AUTH_URL = "https://ticktick.com/oauth/authorize"
TOKEN_URL = "https://ticktick.com/oauth/token"
TOKENS_FILE = Path(__file__).parent / ".tokens.json"

auth_code = None
server_done = threading.Event()


class CallbackHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        global auth_code
        parsed = urlparse(self.path)
        if parsed.path == "/callback":
            params = parse_qs(parsed.query)
            if "code" in params:
                auth_code = params["code"][0]
                self.send_response(200)
                self.send_header("Content-Type", "text/html")
                self.end_headers()
                self.wfile.write(
                    b"<h2>Authorization successful! You can close this tab.</h2>"
                )
            else:
                self.send_response(400)
                self.end_headers()
                self.wfile.write(
                    b"<h2>Authorization failed - no code received.</h2>"
                )
        server_done.set()

    def log_message(self, *args):
        pass


def main():
    client_id = os.getenv("TICKTICK_CLIENT_ID")
    client_secret = os.getenv("TICKTICK_CLIENT_SECRET")

    if not client_id or not client_secret:
        print("ERROR: TICKTICK_CLIENT_ID and TICKTICK_CLIENT_SECRET must be set in .env")
        print("Copy .env.example to .env and fill in your credentials.")
        print("Register your app at: https://developer.ticktick.com/manage")
        return

    params = urlencode(
        {
            "client_id": client_id,
            "response_type": "code",
            "scope": "tasks:read tasks:write",
            "redirect_uri": REDIRECT_URI,
        }
    )
    auth_url = f"{AUTH_URL}?{params}"

    httpd = HTTPServer(("localhost", 8080), CallbackHandler)
    thread = threading.Thread(target=httpd.handle_request)
    thread.start()

    print("Opening TickTick authorization page in your browser...")
    print("If it does not open automatically, visit:")
    print("  " + auth_url)
    print("")
    webbrowser.open(auth_url)

    server_done.wait(timeout=120)
    thread.join(timeout=5)

    if not auth_code:
        print("ERROR: Did not receive authorization code within 2 minutes.")
        return

    print("Authorization code received. Exchanging for tokens...")

    response = httpx.post(
        TOKEN_URL,
        auth=(client_id, client_secret),
        data={
            "code": auth_code,
            "grant_type": "authorization_code",
            "redirect_uri": REDIRECT_URI,
        },
    )

    if response.status_code != 200:
        print("ERROR: Token exchange failed (%d): %s" % (response.status_code, response.text))
        return

    tokens = response.json()
    TOKENS_FILE.write_text(json.dumps(tokens, indent=2))
    print("Tokens saved to " + str(TOKENS_FILE))
    print("Setup complete. You can now use the MCP server.")


if __name__ == "__main__":
    main()
