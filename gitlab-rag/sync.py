import os
import time
import urllib.parse
from pathlib import Path
import requests
 
GITLAB_URL        = os.environ['GITLAB_URL']
GITLAB_TOKEN      = os.environ['GITLAB_TOKEN']
GITLAB_BRANCH     = os.environ.get('GITLAB_BRANCH', 'main')
OPENWEBUI_URL     = os.environ['OPENWEBUI_URL']
OPENWEBUI_TOKEN   = os.environ['OPENWEBUI_TOKEN']
KNOWLEDGE_NAME    = os.environ.get('KNOWLEDGE_NAME', 'gitlab')
SYNC_INTERVAL     = int(os.environ.get('SYNC_INTERVAL', 3600))
MAX_FILE_BYTES    = int(os.environ.get('MAX_FILE_BYTES', 500_000))
GITLAB_VERIFY_SSL = os.environ.get('GITLAB_VERIFY_SSL', 'true').lower() != 'false'
 
# Accept either GITLAB_PROJECT_IDS (comma-separated list) or GITLAB_PROJECT_ID (single).
# Examples:
#   GITLAB_PROJECT_IDS=12,18,42
#   GITLAB_PROJECT_ID=12
_ids_raw = os.environ.get('GITLAB_PROJECT_IDS') or os.environ.get('GITLAB_PROJECT_ID', '')
GITLAB_PROJECT_IDS = [pid.strip() for pid in _ids_raw.split(',') if pid.strip()]
if not GITLAB_PROJECT_IDS:
    raise RuntimeError("Set GITLAB_PROJECT_IDS (comma-separated) or GITLAB_PROJECT_ID")
 
# Extensions that are always binary — skip these
SKIP_EXTENSIONS = {
    '.png', '.jpg', '.jpeg', '.gif', '.bmp', '.ico', '.svg', '.webp',
    '.mp4', '.mp3', '.wav', '.avi', '.mov', '.mkv',
    '.zip', '.tar', '.gz', '.bz2', '.rar', '.7z',
    '.exe', '.dll', '.so', '.dylib', '.bin', '.obj', '.o', '.a',
    '.pdf', '.doc', '.docx', '.xls', '.xlsx', '.ppt', '.pptx',
    '.pyc', '.pyo', '.class', '.jar',
    '.ttf', '.otf', '.woff', '.woff2', '.eot',
    '.db', '.sqlite', '.sqlite3',
}
 
 
def gl_headers():
    return {'PRIVATE-TOKEN': GITLAB_TOKEN}
 
def ow_headers():
    return {'Authorization': f'Bearer {OPENWEBUI_TOKEN}'}
 
 
def gl_get(url, **kwargs):
    """GET against GitLab with shared verify/timeout settings."""
    kwargs.setdefault('timeout', 30)
    kwargs['verify'] = GITLAB_VERIFY_SSL
    return requests.get(url, headers=gl_headers(), **kwargs)
 
 
def get_all_files(project_id):
    files, page = [], 1
    while True:
        r = gl_get(
            f"{GITLAB_URL}/api/v4/projects/{project_id}/repository/tree",
            params={'recursive': 'true', 'per_page': 100, 'page': page, 'ref': GITLAB_BRANCH},
        )
        r.raise_for_status()
        batch = r.json()
        if not batch:
            break
        files.extend(f['path'] for f in batch if f['type'] == 'blob')
        if len(batch) < 100:
            break
        page += 1
    return files
 
 
def is_text(content):
    """Return True if content appears to be text (not binary)."""
    try:
        content.decode('utf-8')
        return True
    except UnicodeDecodeError:
        return False
 
 
def is_lfs_pointer(content):
    """Detect Git LFS pointer files. They're text, but they're not the real content."""
    return content.startswith(b'version https://git-lfs.')
 
 
def should_skip(path):
    return Path(path).suffix.lower() in SKIP_EXTENSIONS
 
 
def fetch_file(project_id, path):
    encoded = urllib.parse.quote(path, safe='')
    r = gl_get(
        f"{GITLAB_URL}/api/v4/projects/{project_id}/repository/files/{encoded}/raw",
        params={'ref': GITLAB_BRANCH},
    )
    if r.ok and len(r.content) <= MAX_FILE_BYTES:
        return r.content
    return None
 
 
def _unwrap_list(payload):
    """OpenWebUI versions vary — sometimes it returns a bare list, sometimes
    {'data': [...]}, sometimes {'knowledge': [...]}. Normalize to a list."""
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        for key in ('data', 'knowledge', 'items', 'results'):
            if isinstance(payload.get(key), list):
                return payload[key]
    return []
 
 
def _unwrap_obj(payload):
    """Same idea for single-object responses — some endpoints wrap in 'data'."""
    if isinstance(payload, dict):
        if isinstance(payload.get('data'), dict):
            return payload['data']
        return payload
    return {}
 
 
def get_or_create_knowledge():
    r = requests.get(f"{OPENWEBUI_URL}/api/v1/knowledge/", headers=ow_headers(), timeout=10)
    r.raise_for_status()
    kbs = _unwrap_list(r.json())
    for kb in kbs:
        if isinstance(kb, dict) and kb.get('name') == KNOWLEDGE_NAME:
            return kb['id']
    r = requests.post(
        f"{OPENWEBUI_URL}/api/v1/knowledge/create",
        headers=ow_headers(),
        json={'name': KNOWLEDGE_NAME, 'description': f'Auto-synced from GitLab projects: {", ".join(GITLAB_PROJECT_IDS)}'},
        timeout=10
    )
    r.raise_for_status()
    obj = _unwrap_obj(r.json())
    if 'id' not in obj:
        raise RuntimeError(f"OpenWebUI create response missing 'id': {r.text[:200]}")
    return obj['id']
 
 
def reset_knowledge(kb_id):
    r = requests.post(f"{OPENWEBUI_URL}/api/v1/knowledge/{kb_id}/reset", headers=ow_headers(), timeout=10)
    r.raise_for_status()
 
 
def upload_and_index(kb_id, project_id, path, content):
    # Prefix filename with project id so files from different repos
    # with identical paths (e.g. README.md) don't collide.
    flat_name = f"{project_id}__" + path.replace('/', '__')
    labelled = f"# Project: {project_id}\n# File: {path}\n\n".encode() + content
 
    try:
        r = requests.post(
            f"{OPENWEBUI_URL}/api/v1/files/",
            headers=ow_headers(),
            files={'file': (flat_name, labelled, 'text/plain')},
            timeout=30
        )
    except requests.RequestException:
        return False
    if not r.ok:
        return False
 
    try:
        file_id = _unwrap_obj(r.json()).get('id')
        if not file_id:
            return False
    except ValueError:
        # Non-JSON response — fail this file gracefully
        return False
 
    try:
        r = requests.post(
            f"{OPENWEBUI_URL}/api/v1/knowledge/{kb_id}/file/add",
            headers=ow_headers(),
            json={'file_id': file_id},
            timeout=10
        )
    except requests.RequestException:
        return False
    return r.ok
 
 
def fetch_all_repos():
    """
    Fetch every file from every configured repo into memory before touching the KB.
    If any repo fails outright (network, auth, 500), abort the whole run so we don't
    reset the KB and leave it half-populated.
 
    Returns a list of (project_id, path, content) tuples.
    """
    collected = []
    for project_id in GITLAB_PROJECT_IDS:
        print(f"--- Pulling project {project_id} ---")
        all_files = get_all_files(project_id)
        print(f"Project {project_id}: {len(all_files)} files. Pulling content...")
 
        kept, skipped = 0, 0
        for path in all_files:
            if should_skip(path):
                skipped += 1
                continue
 
            content = fetch_file(project_id, path)
            if content is None:
                skipped += 1
                continue
 
            if not is_text(content):
                skipped += 1
                continue
 
            if is_lfs_pointer(content):
                # LFS pointer files are text but aren't the real content. Skip.
                skipped += 1
                continue
 
            collected.append((project_id, path, content))
            kept += 1
 
        print(f"--- Project {project_id}: {kept} kept, {skipped} skipped ---")
    return collected
 
 
def push_to_kb(kb_id, items):
    """Upload every collected file to the KB. Logs per-file outcome."""
    ok, failed = 0, 0
    for project_id, path, content in items:
        if upload_and_index(kb_id, project_id, path, content):
            print(f"  OK    [{project_id}] {path}")
            ok += 1
        else:
            print(f"  FAIL  [{project_id}] {path}")
            failed += 1
    return ok, failed
 
 
def sync():
    print(f"=== Starting GitLab -> OpenWebUI sync ({len(GITLAB_PROJECT_IDS)} project(s)) ===")
 
    # 1. Pull everything from GitLab FIRST. If this fails, we never touch the KB.
    #    This means a transient GitLab outage leaves the existing KB intact instead
    #    of wiping it and leaving you with nothing for the next hour.
    try:
        items = fetch_all_repos()
    except Exception as e:
        print(f"ERROR pulling from GitLab — KB left untouched: {e}")
        return
 
    if not items:
        print("WARN: no files collected from any repo. KB left untouched.")
        return
 
    # 2. KB is safe to reset now — we have all the new content in hand.
    kb_id = get_or_create_knowledge()
    reset_knowledge(kb_id)
    print(f"Knowledge base '{KNOWLEDGE_NAME}' reset. Uploading {len(items)} file(s)...")
 
    # 3. Push to KB. Per-file failures here are logged but don't abort.
    ok, failed = push_to_kb(kb_id, items)
 
    print(f"=== Done: {ok} indexed, {failed} failed across {len(GITLAB_PROJECT_IDS)} repo(s). Next sync in {SYNC_INTERVAL}s ===\n")
 
 
if __name__ == '__main__':
    while True:
        try:
            sync()
            time.sleep(SYNC_INTERVAL)
        except Exception as e:
            print(f"ERROR: {e}")
            print("Retrying in 30 seconds...")
            time.sleep(30)
