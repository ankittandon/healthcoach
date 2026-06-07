"""
One-shot helper to connect Whoop for a single user (personal use).

What it does:
  1. Creates (or finds) a Firebase Auth user and seeds the Firestore user doc
     (timezone + chat_state) under studies/{STUDY_ID}/users/{uid}.
  2. Mints a custom token and exchanges it for an ID token via the Auth REST API.
  3. Calls the local backend's GET /whoop/authorize and opens the consent URL
     in your browser. After you approve, the backend stores tokens and runs the
     initial 30-day sync.

Run from the repo root (backend must be running in another terminal):
    conda activate bloom
    APP_ENV=local python scripts/connect_whoop.py
"""

import os
import sys
import json
import time
import webbrowser
import urllib.request

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("APP_ENV", "local")
from dotenv import load_dotenv
load_dotenv(f".env.{os.environ['APP_ENV']}")

EMAIL = "atandon1994@gmail.com"
TIMEZONE = "America/Los_Angeles"

import firebase_admin
from firebase_admin import auth, credentials, firestore

cred = credentials.Certificate(os.getenv("FIREBASE_SERVICE_ACCOUNT_PATH", "backend/serviceAccount.json"))
firebase_admin.initialize_app(cred)
db = firestore.client()

STUDY_ID = os.getenv("STUDY_ID", "testing")
API_KEY = os.environ["FIREBASE_API_KEY"]
PORT = os.getenv("BACKEND_PORT", "5001")


def post_json(url, payload):
    req = urllib.request.Request(url, data=json.dumps(payload).encode(),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req) as resp:
        return json.loads(resp.read())


# 1. User
try:
    user = auth.get_user_by_email(EMAIL)
    print(f"Found existing user: {user.uid}")
except auth.UserNotFoundError:
    user = auth.create_user(email=EMAIL)
    print(f"Created user: {user.uid}")

doc_ref = db.document(f"studies/{STUDY_ID}/users/{user.uid}")
if not doc_ref.get().exists:
    doc_ref.set({"timezone": TIMEZONE, "chat_state": "onboarding"}, merge=True)
    print(f"Seeded user doc (timezone={TIMEZONE})")

# 2. ID token
custom_token = auth.create_custom_token(user.uid).decode()
id_token = post_json(
    f"https://identitytoolkit.googleapis.com/v1/accounts:signInWithCustomToken?key={API_KEY}",
    {"token": custom_token, "returnSecureToken": True},
)["idToken"]
print("Got Firebase ID token")

# 3. Authorize URL from backend
req = urllib.request.Request(f"http://localhost:{PORT}/whoop/authorize",
                             headers={"Authorization": f"Bearer {id_token}"})
with urllib.request.urlopen(req) as resp:
    authorize_url = json.loads(resp.read())["authorize_url"]

print("\nOpening Whoop consent page in your browser...")
print(authorize_url)
webbrowser.open(authorize_url)

print("\nApprove access, then watch the backend logs for 'Whoop sync complete'.")
print("Check status anytime:")
print(f'  curl -H "Authorization: Bearer <token>" http://localhost:{PORT}/whoop/status')
time.sleep(1)
