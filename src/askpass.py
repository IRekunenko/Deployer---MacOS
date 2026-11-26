#!/usr/bin/env python3
"""Deployer's SSH_ASKPASS helper: ssh runs it to obtain a password or key passphrase.
The request goes to Deployer (Keychain, or a password prompt in the app window)."""
import json
import os
import sys
import urllib.request

prompt = sys.argv[1] if len(sys.argv) > 1 else "Password:"
url = os.environ.get("DEPLOYER_ASKPASS_URL")
token = os.environ.get("DEPLOYER_ASKPASS_TOKEN")
if not url or not token:
    sys.exit(1)
req = urllib.request.Request(url, data=json.dumps({"prompt": prompt}).encode(),
                             headers={"Content-Type": "application/json", "X-Askpass-Token": token})
try:
    answer = json.load(urllib.request.urlopen(req, timeout=330)).get("answer")
except Exception:
    sys.exit(1)
if answer is None:
    sys.exit(1)  # cancelled
sys.stdout.write(answer + "\n")
