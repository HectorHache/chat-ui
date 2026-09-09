#!/usr/bin/env python3
"""RAG smoke test (B42) — run me yourself, ~15 s, one model call.

Usage:
  cd ~/Documents/Workspaces/ui
  DATA_DIR=$PWD/data WEBUI_SECRET_KEY=$(cat .webui_secret_key) ./env/bin/python scripts/rag-smoke.py

Expect: PASS — a grounded answer "…360 grams per day [1]" from the Household
collection (dog_care.txt). Any "360 grams" + citation = retrieval works.
"""
import json
import sys
import urllib.request

sys.path.insert(0, "env/lib/python3.11/site-packages")
from open_webui.utils.auth import create_token

COLLECTION_ID = "8d2674fc-39f8-4876-aec7-a283932dd2ca"  # Household
QUESTION = "Khione needs how much dog food per day? Use only our household documents."

token = create_token(data={"id": "4800bc91-2e6b-4a41-82db-65777edcbad7"})
body = {
    "model": "deepseek-v4-flash",
    "stream": False,
    "files": [{"type": "collection", "id": COLLECTION_ID, "name": "Household"}],
    "messages": [{"role": "user", "content": QUESTION}],
    "max_tokens": 400,
}
req = urllib.request.Request(
    "http://127.0.0.1:8390/api/chat/completions",
    data=json.dumps(body).encode(),
    headers={"Authorization": "Bearer " + token, "Content-Type": "application/json"},
    method="POST",
)
with urllib.request.urlopen(req, timeout=240) as r:
    d = json.loads(r.read().decode())
answer = (d["choices"][0]["message"].get("content") or "").strip()
print("ANSWER:", answer[:400])
if "360" in answer and ("[1]" in answer or "document" in answer.lower() or "gram" in answer.lower()):
    print("\nPASS — RAG retrieval works (grounded answer from Household docs).")
else:
    print("\nCHECK — expected '360 grams … [1]'. If you see a refusal, tell the admin.")
