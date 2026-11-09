"""One-shot script to get a Bing Ads OAuth refresh token.

Usage:
    python3 get_bing_refresh_token.py

Opens a browser for Microsoft login, captures the callback on localhost:8080,
exchanges the code for tokens, and prints the refresh token.

Prerequisites:
    http://localhost:8080 must be added as a redirect URI on the Azure app:
    Azure Portal → Microsoft Entra ID → App registrations → jarrah-gads-mcp
    → Authentication → Add a platform → Web → http://localhost:8080
"""
import http.server
import threading
import urllib.parse
import webbrowser
import urllib.request
import json

CLIENT_ID = "f9229c70-7bec-4564-999f-7aa28d5613ab"
CLIENT_SECRET = "REDACTED"
REDIRECT_URI = "http://localhost:8080"
SCOPE = "https://ads.microsoft.com/ads.manage offline_access"

auth_code = None


class CallbackHandler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        global auth_code
        params = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        if "code" in params:
            auth_code = params["code"][0]
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"<h2>Authorised. You can close this tab.</h2>")
        else:
            self.send_response(400)
            self.end_headers()
            error = params.get("error_description", ["Unknown error"])[0]
            self.wfile.write(f"<h2>Error: {error}</h2>".encode())

    def log_message(self, *args):
        pass  # suppress request logs


server = http.server.HTTPServer(("localhost", 8080), CallbackHandler)
thread = threading.Thread(target=server.handle_request)
thread.start()

auth_url = (
    "https://login.microsoftonline.com/common/oauth2/v2.0/authorize"
    f"?client_id={CLIENT_ID}"
    f"&response_type=code"
    f"&redirect_uri={urllib.parse.quote(REDIRECT_URI)}"
    f"&scope={urllib.parse.quote(SCOPE)}"
    "&response_mode=query"
)

print("Opening browser for Microsoft login...")
webbrowser.open(auth_url)

thread.join(timeout=120)
server.server_close()

if not auth_code:
    print("ERROR: No auth code received. Did you complete the login within 2 minutes?")
    exit(1)

print("Auth code received. Exchanging for tokens...")

data = urllib.parse.urlencode({
    "client_id": CLIENT_ID,
    "client_secret": CLIENT_SECRET,
    "code": auth_code,
    "redirect_uri": REDIRECT_URI,
    "grant_type": "authorization_code",
}).encode()

req = urllib.request.Request(
    "https://login.microsoftonline.com/common/oauth2/v2.0/token",
    data=data,
    method="POST",
)
try:
    with urllib.request.urlopen(req) as r:
        tokens = json.loads(r.read())
except urllib.error.HTTPError as e:
    print(f"ERROR: Token exchange failed: {e.read().decode()}")
    exit(1)
print("\n✅ Success! Add this to your .env and Fly.io secrets:\n")
print(f"BING_REFRESH_TOKEN={tokens['refresh_token']}")
