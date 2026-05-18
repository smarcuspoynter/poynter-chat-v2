"""
Run this once to authorize Google Drive access.
It will open a browser window — sign in with your @poynter.org account.
After authorizing, a token.json file is saved. The main app uses that file.

Usage:
    python authenticate_drive.py
"""
import os
from google_auth_oauthlib.flow import InstalledAppFlow

SCOPES = [
    "https://www.googleapis.com/auth/drive.readonly",
    "https://www.googleapis.com/auth/cloud_search.query",
]
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CREDENTIALS_PATH = os.path.join(BASE_DIR, "credentials.json")
TOKEN_PATH = os.path.join(BASE_DIR, "token.json")

if not os.path.exists(CREDENTIALS_PATH):
    print(f"Error: credentials.json not found at {CREDENTIALS_PATH}")
    print("Download it from Google Cloud Console → APIs & Services → Credentials")
    exit(1)

flow = InstalledAppFlow.from_client_secrets_file(CREDENTIALS_PATH, SCOPES)
creds = flow.run_local_server(port=0)

with open(TOKEN_PATH, "w") as f:
    f.write(creds.to_json())

print(f"\nSuccess! token.json saved to {TOKEN_PATH}")
print("You can now run the app: streamlit run app.py")
