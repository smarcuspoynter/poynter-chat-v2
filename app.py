import base64
import hashlib
import os
import re
import json
import secrets
import uuid
import urllib.parse
import urllib.request
from datetime import date
import streamlit as st
import anthropic
from concurrent.futures import ThreadPoolExecutor, as_completed
import requests
from curl_cffi import requests as cffi_requests
from dotenv import load_dotenv
from qdrant_client import QdrantClient
from qdrant_client.models import Distance, VectorParams, PointStruct, Filter, FieldCondition, MatchValue, PointIdsList, PayloadSchemaType
from sentence_transformers import SentenceTransformer

try:
    from googleapiclient.discovery import build
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    DRIVE_AVAILABLE = True
except ImportError:
    DRIVE_AVAILABLE = False

load_dotenv()

# --- Config ---
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY")
LD_BASE_URL = "https://www.poynter.org"
LD_USERNAME = os.environ.get("LD_USERNAME", "ld_api_read")
LD_APP_PASSWORD = os.environ.get("LD_APP_PASSWORD", "")
LD_AUTH = (LD_USERNAME, LD_APP_PASSWORD)
MODEL = "claude-sonnet-4-6"
DOCS_FILE = os.path.join(os.path.dirname(__file__), "documents.json")
AUDITOR_FILE = os.path.join(os.path.dirname(__file__), "auditor.json")
AUDIT_PAGE_SIZE = 25
DRIVE_SCOPES = [
    "https://www.googleapis.com/auth/drive.readonly",
    "https://www.googleapis.com/auth/cloud_search.query",
]
TOKEN_PATH = os.path.join(os.path.dirname(__file__), "token.json")
CREDENTIALS_PATH = os.path.join(os.path.dirname(__file__), "credentials.json")



REDIRECT_URI = os.environ.get("REDIRECT_URI", "http://localhost:8501")
GOOGLE_CLIENT_ID = os.environ.get("GOOGLE_CLIENT_ID", "")
GOOGLE_CLIENT_SECRET = os.environ.get("GOOGLE_CLIENT_SECRET", "")
GOOGLE_AUTH_ENDPOINT = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_ENDPOINT = "https://oauth2.googleapis.com/token"
GOOGLE_USERINFO_ENDPOINT = "https://www.googleapis.com/oauth2/v2/userinfo"


def _pkce_pair():
    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode()).digest()
    ).rstrip(b"=").decode()
    return verifier, challenge


def build_google_auth_url() -> str:
    verifier, challenge = _pkce_pair()
    state = base64.urlsafe_b64encode(
        json.dumps({"v": verifier, "n": secrets.token_hex(8)}).encode()
    ).decode()
    params = {
        "client_id": GOOGLE_CLIENT_ID,
        "redirect_uri": REDIRECT_URI,
        "response_type": "code",
        "scope": "openid email profile",
        "state": state,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
    }
    return GOOGLE_AUTH_ENDPOINT + "?" + urllib.parse.urlencode(params)


def fetch_google_user_info(code: str, state: str) -> dict:
    state_data = json.loads(base64.urlsafe_b64decode(state + "==").decode())
    verifier = state_data["v"]
    body = urllib.parse.urlencode({
        "code": code,
        "client_id": GOOGLE_CLIENT_ID,
        "client_secret": GOOGLE_CLIENT_SECRET,
        "redirect_uri": REDIRECT_URI,
        "grant_type": "authorization_code",
        "code_verifier": verifier,
    }).encode()
    req = urllib.request.Request(GOOGLE_TOKEN_ENDPOINT, data=body, method="POST")
    try:
        with urllib.request.urlopen(req) as r:
            token = json.loads(r.read())
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"Token exchange failed ({e.code}): {e.read().decode()}")
    req2 = urllib.request.Request(
        GOOGLE_USERINFO_ENDPOINT,
        headers={"Authorization": f"Bearer {token['access_token']}"},
    )
    with urllib.request.urlopen(req2) as r:
        user_info = json.loads(r.read())
    return {
        "email": user_info.get("email", ""),
        "access_token": token.get("access_token", ""),
        "refresh_token": token.get("refresh_token", ""),
    }


DRIVE_MIME_MAP = {
    "Docs": "application/vnd.google-apps.document",
    "Slides": "application/vnd.google-apps.presentation",
    "Sheets": "application/vnd.google-apps.spreadsheet",
    "Forms": "application/vnd.google-apps.form",
}

DRIVE_MIME_LABELS = {
    "application/vnd.google-apps.document": "Google Doc",
    "application/vnd.google-apps.spreadsheet": "Google Sheet",
    "application/vnd.google-apps.presentation": "Google Slides",
    "application/vnd.google-apps.folder": "Folder",
    "application/pdf": "PDF",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": "Word Doc",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": "Excel",
}


def build_drive_type_filter(drive_types):
    if not drive_types:
        return ""
    conditions = []
    for ft in drive_types:
        if ft == "Images":
            conditions.append("mimeType contains 'image/'")
        elif ft in DRIVE_MIME_MAP:
            conditions.append(f"mimeType = '{DRIVE_MIME_MAP[ft]}'")
    if not conditions:
        return ""
    return " and (" + " or ".join(conditions) + ")"


def load_documents():
    user_email = st.session_state.get("user_email", "")
    if not user_email:
        return []
    try:
        _ensure_collections()
        client = get_qdrant()
        points, _ = client.scroll(
            DOCS_COLLECTION,
            scroll_filter=Filter(must=[FieldCondition(key="user_email", match=MatchValue(value=user_email))]),
            limit=1000,
            with_payload=True,
        )
        return [p.payload for p in points]
    except Exception as e:
        st.session_state["_storage_error"] = f"Could not load documents: {e}"
        return []


def persist_documents():
    user_email = st.session_state.get("user_email", "")
    if not user_email:
        return
    try:
        _ensure_collections()
        client = get_qdrant()
        current_docs = st.session_state.documents
        for doc in current_docs:
            if "id" not in doc:
                doc["id"] = str(uuid.uuid4())
        current_ids = {doc["id"] for doc in current_docs}
        existing_points, _ = client.scroll(
            DOCS_COLLECTION,
            scroll_filter=Filter(must=[FieldCondition(key="user_email", match=MatchValue(value=user_email))]),
            limit=1000,
            with_payload=False,
        )
        existing_ids = {str(p.id) for p in existing_points}
        if current_docs:
            client.upsert(
                DOCS_COLLECTION,
                points=[
                    PointStruct(
                        id=doc["id"],
                        vector=[0.0],
                        payload={**doc, "user_email": user_email},
                    )
                    for doc in current_docs
                ],
            )
        to_delete = existing_ids - current_ids
        if to_delete:
            client.delete(DOCS_COLLECTION, points_selector=PointIdsList(points=list(to_delete)))
    except Exception as e:
        st.toast(f"⚠️ Document save failed: {e}", icon="⚠️")


def load_auditor_results() -> dict:
    try:
        _ensure_collections()
        client = get_qdrant()
        points, _ = client.scroll(AUDIT_COLLECTION, limit=10000, with_payload=True)
        return {str(p.payload["course_id"]): p.payload for p in points if "course_id" in p.payload}
    except Exception as e:
        st.session_state["_storage_error"] = f"Could not load audit results: {e}"
        return {}


def persist_auditor_results():
    try:
        _ensure_collections()
        client = get_qdrant()
        current = st.session_state.auditor_results
        if not current:
            return
        client.upsert(
            AUDIT_COLLECTION,
            points=[
                PointStruct(
                    id=int(cid),
                    vector=[0.0],
                    payload=result,
                )
                for cid, result in current.items()
            ],
        )
    except Exception as e:
        st.toast(f"⚠️ Audit save failed: {e}", icon="⚠️")


@st.cache_data(ttl=3600)
def fetch_all_courses_cached():
    try:
        def fetch_page(page_num):
            data, _ = ld_get("sfwd-courses", {"status": "publish", "per_page": 100, "page": page_num})
            return data

        first, headers = ld_get("sfwd-courses", {"status": "publish", "per_page": 100, "page": 1})
        total_pages = int(headers.get("X-WP-TotalPages", 1))

        all_raw = list(first)
        if total_pages > 1:
            with ThreadPoolExecutor() as executor:
                futures = {executor.submit(fetch_page, p): p for p in range(2, total_pages + 1)}
                page_data = {}
                for future in as_completed(futures):
                    page_data[futures[future]] = future.result()
                for p in range(2, total_pages + 1):
                    all_raw.extend(page_data[p])

        return [
            {
                "id": str(c["id"]),
                "title": strip_html(c.get("title", {}).get("rendered", "(no title)")),
                "link": c.get("link", ""),
                "modified": c.get("modified", "")[:10],
            }
            for c in all_raw
        ]
    except Exception as e:
        st.session_state["ld_fetch_error"] = str(e)
        return []


def run_course_audit(course_id: str, course_title: str) -> dict:
    structure = tool_get_course_structure(int(course_id))
    lessons = tool_list_lessons(int(course_id))
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    prompt = f"""Audit this Poynter online journalism course and return a structured JSON report.

Course: {course_title}

Structure:
{structure}

Lessons:
{lessons}

Return ONLY valid JSON with exactly these fields — no other text:
{{
  "lift": <int 0-100, effort needed: 0=no changes, 100=full rewrite>,
  "relevance": <int 0-100, importance to modern journalism practice>,
  "level": <"Low" | "Medium" | "High" — Low if lift<31, High if lift>65>,
  "quarter": <"High Priority" | "Medium Priority" | "Low Priority">,
  "issues": [
    {{"type": <"Outdated" | "Missing_AI" | "Broken_Link" | "Quality">, "loc": "<lesson or section>", "desc": "<specific issue>"}}
  ],
  "summary": "<2-3 sentences: current state and top recommended action>"
}}

Scheduling: High Priority = relevance ≥ 70; Medium Priority = relevance 40-69 and lift ≤ 65; Low Priority = everything else.
Flag Missing_AI if no lesson covers generative AI, AI tools, or automation in journalism.
Flag Outdated if content references tools, platforms, or statistics that appear pre-2022."""
    resp = client.messages.create(
        model=MODEL,
        max_tokens=4096,
        messages=[{"role": "user", "content": prompt}],
    )
    text = resp.content[0].text.strip()
    text = re.sub(r"^```[a-z]*\n?", "", text)
    text = re.sub(r"\n?```$", "", text)
    try:
        result = json.loads(text)
    except Exception as e:
        result = {
            "lift": 50, "relevance": 50, "level": "Medium",
            "quarter": "Low Priority", "issues": [],
            "summary": f"Audit parse error: {e}. Raw response: {text[:300]}",
        }
    result["course_id"] = course_id
    result["title"] = course_title
    return result


SYSTEM_PROMPT = """You are a helpful assistant for Poynter's teaching team.
You have access to Poynter's full LearnDash course catalog AND the team's Google Drive.

Help the team:
- Find content on any topic for reuse or reference
- Understand what exists in the catalog and team documents
- Review specific lessons or topics for staleness, relevance, or quality

Respond in whatever format best fits the question. A broad question gets an overview.
A specific content question gets a detailed breakdown. A review request gets structured feedback with specific quotes and suggestions.

When finding content on a topic: call search_all — it automatically searches every enabled
source (LMS and Drive) in parallel and returns results in labeled sections.

For comprehensive questions (e.g. "show me everything on X", "evaluate consistency across content"):
- Call search_all multiple times with different phrasings of the topic to maximize coverage
- After identifying relevant lessons or topics from search results, call get_lesson or get_topic
  to read the full text before drawing conclusions about quality or consistency
- Do not summarize based only on titles and snippets for evaluation tasks

For LMS navigation: use search_courses when looking for a specific course by name.
Drill into results with get_course_structure, list_lessons, or list_topics as needed.

Always include direct links to any course, lesson, topic, or Drive file you reference.
Links are provided in the tool results. Format them as markdown links, e.g. [Title](https://...).
Never construct or guess a URL — only use URLs that appear verbatim in tool results."""

QDRANT_URL = os.environ.get("QDRANT_URL", "")
QDRANT_API_KEY = os.environ.get("QDRANT_API_KEY", "")
QDRANT_COLLECTION = "poynter_teaching"
DOCS_COLLECTION = "user_documents"
AUDIT_COLLECTION = "audit_results"


@st.cache_resource
def get_qdrant():
    return QdrantClient(url=QDRANT_URL, api_key=QDRANT_API_KEY)


def _ensure_collections():
    if st.session_state.get("_collections_ready"):
        return
    client = get_qdrant()
    existing = {c.name for c in client.get_collections().collections}
    if DOCS_COLLECTION not in existing:
        client.create_collection(
            DOCS_COLLECTION,
            vectors_config=VectorParams(size=1, distance=Distance.COSINE),
        )
    # Always ensure index exists — idempotent, handles collections created before this was added
    client.create_payload_index(
        DOCS_COLLECTION,
        field_name="user_email",
        field_schema=PayloadSchemaType.KEYWORD,
    )
    if AUDIT_COLLECTION not in existing:
        client.create_collection(
            AUDIT_COLLECTION,
            vectors_config=VectorParams(size=1, distance=Distance.COSINE),
        )
    st.session_state["_collections_ready"] = True


@st.cache_resource
def load_search_models():
    dense = SentenceTransformer("sentence-transformers/all-MiniLM-L6-v2")
    qdrant = QdrantClient(url=QDRANT_URL, api_key=QDRANT_API_KEY)
    return dense, qdrant


# --- LearnDash API ---

def ld_get(path, params=None):
    url = f"{LD_BASE_URL}/wp-json/ldlms/v2/{path}"
    resp = cffi_requests.get(url, auth=LD_AUTH, params=params or {}, timeout=15, impersonate="chrome")
    resp.raise_for_status()
    return resp.json(), resp.headers


def get_all_pages(path, extra_params=None):
    params = {"per_page": 100, "page": 1}
    if extra_params:
        params.update(extra_params)
    results = []
    while True:
        data, headers = ld_get(path, params)
        results.extend(data)
        total_pages = int(headers.get("X-WP-TotalPages", 1))
        if params["page"] >= total_pages:
            break
        params["page"] += 1
    return results


def strip_html(html):
    if not html:
        return ""
    text = re.sub(r"\[/?[^\]]+\]", " ", html)
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


# --- Tool implementations ---

def tool_search_content(query: str) -> str:
    lines = [f"Search results for '{query}':\n"]
    seen_urls = set()

    # Semantic search via Qdrant
    try:
        dense_model, qdrant = load_search_models()
        dense_vec = dense_model.encode(query).tolist()
        results = qdrant.query_points(
            collection_name=QDRANT_COLLECTION,
            query=dense_vec,
            using="dense",
            limit=20,
            with_payload=True,
        )
        seen_docs = set()
        for point in results.points:
            p = point.payload
            doc_id = p.get("doc_id", "")
            if doc_id in seen_docs:
                continue
            seen_docs.add(doc_id)
            title = p.get("title", "(no title)")
            content_type = p.get("content_type", "")
            parent_title = p.get("parent_title", "")
            url = p.get("url", "")
            snippet = (p.get("description", "") or p.get("chunk_text", ""))[:120]
            line = f"- [{content_type.upper()}] {title}"
            if parent_title:
                line += f" (in: {parent_title})"
            if url:
                line += f" — URL: {url}"
                seen_urls.add(url)
            if snippet:
                line += f" | {snippet}"
            lines.append(line)
    except Exception as e:
        lines.append(f"(Semantic search error: {e})")

    # Keyword search via LearnDash API — catches content not in Qdrant index
    try:
        kw_results = []
        with ThreadPoolExecutor() as ex:
            f_lessons = ex.submit(get_all_pages, "sfwd-lessons", {"search": query, "status": "publish", "per_page": 20})
            f_topics = ex.submit(get_all_pages, "sfwd-topic", {"search": query, "status": "publish", "per_page": 20})
            for item in f_lessons.result():
                title = strip_html(item.get("title", {}).get("rendered", "(no title)"))
                url = item.get("link", "")
                if url not in seen_urls:
                    kw_results.append(f"- [LESSON] {title} — URL: {url}")
                    seen_urls.add(url)
            for item in f_topics.result():
                title = strip_html(item.get("title", {}).get("rendered", "(no title)"))
                url = item.get("link", "")
                if url not in seen_urls:
                    kw_results.append(f"- [TOPIC] {title} — URL: {url}")
                    seen_urls.add(url)
        if kw_results:
            lines.append("\n(Additional keyword matches from LMS:)")
            lines.extend(kw_results)
    except Exception:
        pass

    if len(lines) == 1:
        return f"No results found for '{query}'."
    return "\n".join(lines)


def tool_search_courses(query: str) -> str:
    try:
        courses = get_all_pages("sfwd-courses", {"search": query, "status": "publish"})
        if not courses:
            return f"No courses found matching '{query}'."
        lines = [f"Found {len(courses)} course(s) matching '{query}':\n"]
        for c in courses:
            title = strip_html(c.get("title", {}).get("rendered", "(no title)"))
            link = c.get("link", "")
            line = f"- [ID {c['id']}] {title}"
            if link:
                line += f" — {link}"
            lines.append(line)
        return "\n".join(lines)
    except Exception as e:
        return f"Error searching courses: {e}"


def tool_list_courses() -> str:
    try:
        courses = get_all_pages("sfwd-courses", {"status": "publish"})
        if not courses:
            return "No courses found."
        lines = [f"Found {len(courses)} published courses:\n"]
        for c in courses:
            title = strip_html(c.get("title", {}).get("rendered", "(no title)"))
            link = c.get("link", "")
            line = f"- [ID {c['id']}] {title}"
            if link:
                line += f" — {link}"
            lines.append(line)
        return "\n".join(lines)
    except Exception as e:
        return f"Error listing courses: {e}"


def tool_get_course_structure(course_id: int) -> str:
    try:
        data, _ = ld_get(f"sfwd-courses/{course_id}/steps")
        if not data:
            return f"No structure found for course {course_id}."
        lines = [f"Structure for course {course_id}:\n"]
        for step in data:
            step_type = step.get("type", "unknown")
            title = strip_html(step.get("title", {}).get("rendered", "(no title)"))
            step_id = step.get("id", "")
            link = step.get("link", "")
            entry = f"  [{step_type} ID {step_id}] {title}"
            if link:
                entry += f" — {link}"
            lines.append(entry)
            for child in step.get("steps", []):
                child_type = child.get("type", "unknown")
                child_title = strip_html(child.get("title", {}).get("rendered", "(no title)"))
                child_id = child.get("id", "")
                child_link = child.get("link", "")
                child_entry = f"    └─ [{child_type} ID {child_id}] {child_title}"
                if child_link:
                    child_entry += f" — {child_link}"
                lines.append(child_entry)
        return "\n".join(lines)
    except Exception as e:
        return f"Error getting course structure: {e}"


def tool_list_lessons(course_id: int) -> str:
    try:
        lessons = get_all_pages("sfwd-lessons", {"course": course_id})
        if not lessons:
            return f"No lessons found for course {course_id}."
        lines = [f"Lessons in course {course_id}:\n"]
        for l in lessons:
            title = strip_html(l.get("title", {}).get("rendered", "(no title)"))
            link = l.get("link", "")
            line = f"- [ID {l['id']}] {title}"
            if link:
                line += f" — {link}"
            lines.append(line)
        return "\n".join(lines)
    except Exception as e:
        return f"Error listing lessons: {e}"


def tool_list_topics(course_id: int, lesson_id: int) -> str:
    try:
        topics = get_all_pages("sfwd-topic", {"course": course_id, "lesson": lesson_id})
        if not topics:
            return f"No topics found for lesson {lesson_id}."
        lines = [f"Topics in lesson {lesson_id}:\n"]
        for t in topics:
            title = strip_html(t.get("title", {}).get("rendered", "(no title)"))
            link = t.get("link", "")
            line = f"- [ID {t['id']}] {title}"
            if link:
                line += f" — {link}"
            lines.append(line)
        return "\n".join(lines)
    except Exception as e:
        return f"Error listing topics: {e}"


def tool_get_lesson(lesson_id: int) -> str:
    try:
        data, _ = ld_get(f"sfwd-lessons/{lesson_id}")
        title = strip_html(data.get("title", {}).get("rendered", "(no title)"))
        content = strip_html(data.get("content", {}).get("rendered", "(no content)"))
        link = data.get("link", "")
        header = f"Lesson: {title}"
        if link:
            header += f"\nURL: {link}"
        return f"{header}\n\n{content}"
    except Exception as e:
        return f"Error getting lesson: {e}"


def tool_get_topic(topic_id: int) -> str:
    try:
        data, _ = ld_get(f"sfwd-topic/{topic_id}")
        title = strip_html(data.get("title", {}).get("rendered", "(no title)"))
        content = strip_html(data.get("content", {}).get("rendered", "(no content)"))
        link = data.get("link", "")
        header = f"Topic: {title}"
        if link:
            header += f"\nURL: {link}"
        return f"{header}\n\n{content}"
    except Exception as e:
        return f"Error getting topic: {e}"


def get_drive_credentials():
    if not DRIVE_AVAILABLE:
        return None
    # Use the token from the user's Google sign-in (works on Streamlit Cloud)
    access_token = st.session_state.get("google_access_token")
    if access_token:
        return Credentials(
            token=access_token,
            refresh_token=st.session_state.get("google_refresh_token") or None,
            token_uri=GOOGLE_TOKEN_ENDPOINT,
            client_id=GOOGLE_CLIENT_ID,
            client_secret=GOOGLE_CLIENT_SECRET,
            scopes=["https://www.googleapis.com/auth/drive.readonly"],
        )
    # Fallback: local token.json (development only)
    if not os.path.exists(TOKEN_PATH):
        return None
    creds = Credentials.from_authorized_user_file(TOKEN_PATH, DRIVE_SCOPES)
    if not creds.valid:
        if creds.expired and creds.refresh_token:
            creds.refresh(Request())
            with open(TOKEN_PATH, "w") as f:
                f.write(creds.to_json())
        else:
            return None
    return creds


CLOUD_SEARCH_TYPE_MAP = {
    "Docs": "type:document",
    "Slides": "type:presentation",
    "Sheets": "type:spreadsheet",
    "Forms": "type:form",
    "Images": "type:image",
}


def _tool_search_drive_cloudsearch(creds, query: str) -> str:
    drive_types = st.session_state.get("drive_types_select", ["Docs", "Slides", "Sheets"])
    type_filters = [CLOUD_SEARCH_TYPE_MAP[t] for t in drive_types if t in CLOUD_SEARCH_TYPE_MAP]

    full_query = query
    if type_filters:
        full_query += " (" + " OR ".join(type_filters) + ")"

    body = {
        "query": full_query,
        "requestOptions": {"searchApplicationId": "searchapplications/default"},
        "pageSize": 10,
    }

    service = build("cloudsearch", "v1", credentials=creds)
    response = service.query().search(body=body).execute()
    items = response.get("results", [])

    if not items:
        return f"No Drive files found matching '{query}'."

    lines = [f"Google Drive results for '{query}' (org-wide):\n"]
    for item in items:
        title = item.get("title", "(unnamed)")
        url = item.get("url", "")
        mime = item.get("metadata", {}).get("mimeType", "")
        label = DRIVE_MIME_LABELS.get(mime, "File")
        line = f"- [{label}] {title}"
        if url:
            line += f" — {url}"
        lines.append(line)
    return "\n".join(lines)


def _tool_search_drive_api(creds, query: str) -> str:
    service = build("drive", "v3", credentials=creds)
    safe_query = query.replace("'", "\\'")

    drive_types = st.session_state.get("drive_types_select", ["Docs", "Slides", "Sheets"])
    type_filter = build_drive_type_filter(drive_types)

    date_filter = ""
    date_from = st.session_state.get("date_from")
    date_to = st.session_state.get("date_to")
    if date_from:
        date_filter += f" and modifiedTime >= '{date_from.isoformat()}T00:00:00'"
    if date_to:
        date_filter += f" and modifiedTime <= '{date_to.isoformat()}T23:59:59'"

    drive_query = f"fullText contains '{safe_query}' and trashed=false{type_filter}{date_filter}"

    results = service.files().list(
        q=drive_query,
        spaces="drive",
        fields="files(id, name, mimeType, webViewLink, modifiedTime)",
        pageSize=10,
        orderBy="modifiedTime desc",
        includeItemsFromAllDrives=True,
        supportsAllDrives=True,
        corpora="allDrives",
    ).execute()
    files = results.get("files", [])
    if not files:
        return f"No Drive files found matching '{query}'."
    lines = [f"Google Drive results for '{query}':\n"]
    for item in files:
        name = item.get("name", "(unnamed)")
        link = item.get("webViewLink", "")
        mime = DRIVE_MIME_LABELS.get(item.get("mimeType", ""), "File")
        line = f"- [{mime}] {name}"
        if link:
            line += f" — {link}"
        lines.append(line)
    return "\n".join(lines)


def tool_search_drive(query: str, access_token: str = "", drive_types: list = None, date_from=None, date_to=None) -> str:
    # All session_state values must be passed as args — this runs in a thread where session_state is unavailable
    if not access_token:
        return "Google Drive search is not connected: no access token in session. Sign out and sign back in to enable Drive search."

    if drive_types is None:
        drive_types = ["Docs", "Slides", "Sheets"]

    safe_query = query.replace("'", "\\'")
    type_filter = build_drive_type_filter(drive_types)

    date_filter = ""
    if date_from:
        date_filter += f" and modifiedTime >= '{date_from.isoformat()}T00:00:00'"
    if date_to:
        date_filter += f" and modifiedTime <= '{date_to.isoformat()}T23:59:59'"

    drive_query = f"fullText contains '{safe_query}' and trashed=false{type_filter}{date_filter}"

    try:
        resp = requests.get(
            "https://www.googleapis.com/drive/v3/files",
            headers={"Authorization": f"Bearer {access_token}"},
            params={
                "q": drive_query,
                "spaces": "drive",
                "fields": "files(id,name,mimeType,webViewLink,modifiedTime)",
                "pageSize": 20,
                "orderBy": "modifiedTime desc",
                "includeItemsFromAllDrives": "true",
                "supportsAllDrives": "true",
                "corpora": "allDrives",
            },
            timeout=15,
        )
        if resp.status_code != 200:
            return f"Drive API error {resp.status_code}: {resp.text[:300]}"
        files = resp.json().get("files", [])
        if not files:
            return f"No Drive files found matching '{query}'."
        lines = [f"Google Drive results for '{query}':\n"]
        for item in files:
            name = item.get("name", "(unnamed)")
            link = item.get("webViewLink", "")
            mime = DRIVE_MIME_LABELS.get(item.get("mimeType", ""), "File")
            line = f"- [{mime}] {name}"
            if link:
                line += f" — {link}"
            lines.append(line)
        return "\n".join(lines)
    except Exception as e:
        return f"Drive search error: {e}"


def tool_search_all(query: str, context: dict = None) -> str:
    ctx = context or {}
    src_lms = ctx.get("src_lms", st.session_state.get("src_lms", True))
    src_drive = ctx.get("src_drive", st.session_state.get("src_drive", True))
    access_token = ctx.get("access_token", "")
    drive_types = ctx.get("drive_types", ["Docs", "Slides", "Sheets"])
    date_from = ctx.get("date_from")
    date_to = ctx.get("date_to")

    futures = {}
    with ThreadPoolExecutor() as executor:
        if src_lms:
            futures["lms"] = executor.submit(tool_search_content, query)
        if src_drive:
            futures["drive"] = executor.submit(tool_search_drive, query, access_token, drive_types, date_from, date_to)

    parts = []
    if "lms" in futures:
        parts.append("**📚 Course Content**\n" + futures["lms"].result())
    if "drive" in futures:
        parts.append("**📁 Team Resources**\n" + futures["drive"].result())

    return "\n\n".join(parts) if parts else "No search sources are enabled."


def execute_tool(name: str, inputs: dict, context: dict = None) -> str:
    ctx = context or {}
    if name == "search_content":
        return tool_search_content(inputs["query"])
    elif name == "search_courses":
        return tool_search_courses(inputs["query"])
    elif name == "list_courses":
        return tool_list_courses()
    elif name == "get_course_structure":
        return tool_get_course_structure(inputs["course_id"])
    elif name == "list_lessons":
        return tool_list_lessons(inputs["course_id"])
    elif name == "list_topics":
        return tool_list_topics(inputs["course_id"], inputs["lesson_id"])
    elif name == "get_lesson":
        return tool_get_lesson(inputs["lesson_id"])
    elif name == "get_topic":
        return tool_get_topic(inputs["topic_id"])
    elif name == "search_all":
        return tool_search_all(inputs["query"], ctx)
    return f"Unknown tool: {name}"


# --- Tool definitions ---

SEARCH_ALL_TOOL = {
    "name": "search_all",
    "description": "Search all active sources (Poynter LMS and/or Google Drive) for a topic. Always call this for any topic-based question — it automatically searches every enabled source in parallel.",
    "input_schema": {
        "type": "object",
        "properties": {"query": {"type": "string", "description": "Topic or concept to search for"}},
        "required": ["query"],
    },
}

LMS_NAV_TOOLS = [
    {
        "name": "search_courses",
        "description": "Search for courses by keyword. Use when looking for a specific course by name.",
        "input_schema": {
            "type": "object",
            "properties": {"query": {"type": "string", "description": "The search term or topic to look for"}},
            "required": ["query"],
        },
    },
    {
        "name": "list_courses",
        "description": "List all published courses in the catalog. Use only when the user wants a full overview — this can return hundreds of courses.",
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "get_course_structure",
        "description": "Get the full lesson and topic structure of a specific course.",
        "input_schema": {
            "type": "object",
            "properties": {"course_id": {"type": "integer", "description": "The LearnDash course ID"}},
            "required": ["course_id"],
        },
    },
    {
        "name": "list_lessons",
        "description": "List all lessons in a course.",
        "input_schema": {
            "type": "object",
            "properties": {"course_id": {"type": "integer", "description": "The LearnDash course ID"}},
            "required": ["course_id"],
        },
    },
    {
        "name": "list_topics",
        "description": "List all topics within a specific lesson.",
        "input_schema": {
            "type": "object",
            "properties": {
                "course_id": {"type": "integer", "description": "The LearnDash course ID"},
                "lesson_id": {"type": "integer", "description": "The lesson ID"},
            },
            "required": ["course_id", "lesson_id"],
        },
    },
    {
        "name": "get_lesson",
        "description": "Get the full text content of a specific lesson.",
        "input_schema": {
            "type": "object",
            "properties": {"lesson_id": {"type": "integer", "description": "The lesson ID"}},
            "required": ["lesson_id"],
        },
    },
    {
        "name": "get_topic",
        "description": "Get the full text content of a specific topic.",
        "input_schema": {
            "type": "object",
            "properties": {"topic_id": {"type": "integer", "description": "The topic ID"}},
            "required": ["topic_id"],
        },
    },
]


def get_active_tools():
    src_lms = st.session_state.get("src_lms", True)
    src_drive = st.session_state.get("src_drive", True)
    tools = []
    if src_lms or src_drive:
        tools.append(SEARCH_ALL_TOOL)
    if src_lms:
        tools.extend(LMS_NAV_TOOLS)
    return tools


def build_filter_system_addendum():
    src_lms = st.session_state.get("src_lms", True)
    src_drive = st.session_state.get("src_drive", True)

    if not src_lms and not src_drive:
        return "\n\nNo search sources are currently enabled. Tell the user they need to enable at least one source in the filters."

    lines = []
    active = [s for s, on in [("Poynter LMS", src_lms), ("Google Drive", src_drive)] if on]
    lines.append(f"Active search sources: {', '.join(active)}.")

    if src_drive:
        drive_types = st.session_state.get("drive_types_select", ["Docs", "Slides", "Sheets"])
        if drive_types:
            lines.append(f"Drive search is restricted to file types: {', '.join(drive_types)}.")

    date_from = st.session_state.get("date_from")
    date_to = st.session_state.get("date_to")
    if date_from or date_to:
        parts = ["Date filter active"]
        if date_from:
            parts.append(f"from {date_from}")
        if date_to:
            parts.append(f"to {date_to}")
        lines.append(" ".join(parts) + ".")

    if not src_lms:
        lines.append("Do NOT use any LMS navigation tools.")
    if not src_drive:
        lines.append("Do NOT search Google Drive.")

    return "\n\n" + " ".join(lines)


# --- Claude conversation loop (streaming) ---

TOOL_LABELS = {
    "search_all": "Searching all sources...",
    "search_content": "Searching course content...",
    "search_courses": "Searching courses...",
    "list_courses": "Loading course catalog...",
    "get_course_structure": "Loading course structure...",
    "list_lessons": "Loading lessons...",
    "list_topics": "Loading topics...",
    "get_lesson": "Loading lesson...",
    "get_topic": "Loading topic...",
    "search_drive": "Searching Google Drive...",
}


def run_claude_streaming(messages: list, placeholder, active_tools: list, system_addendum: str = "") -> str:
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    api_messages = [{"role": m["role"], "content": m["content"]} for m in messages]

    system_text = SYSTEM_PROMPT + system_addendum
    cached_system = [{"type": "text", "text": system_text, "cache_control": {"type": "ephemeral"}}]

    if active_tools:
        cached_tools = active_tools[:-1] + [{**active_tools[-1], "cache_control": {"type": "ephemeral"}}]
    else:
        cached_tools = []

    full_text = ""

    while True:
        kwargs = dict(model=MODEL, max_tokens=4096, system=cached_system, messages=api_messages)
        if cached_tools:
            kwargs["tools"] = cached_tools

        with client.messages.stream(**kwargs) as stream:
            for text in stream.text_stream:
                full_text += text
                placeholder.markdown(full_text + "▌")
            final = stream.get_final_message()

        if final.stop_reason == "end_turn":
            return full_text

        if final.stop_reason == "tool_use":
            tool_blocks = [b for b in final.content if b.type == "tool_use"]
            labels = [TOOL_LABELS.get(b.name, f"Running {b.name}...") for b in tool_blocks]
            status = " · ".join(f"*{l}*" for l in labels)
            placeholder.markdown((full_text + f"\n\n{status}") if full_text else status)

            tool_context = {
                "src_lms": st.session_state.get("src_lms", True),
                "src_drive": st.session_state.get("src_drive", True),
                "access_token": st.session_state.get("google_access_token", ""),
                "drive_types": st.session_state.get("drive_types_select", ["Docs", "Slides", "Sheets"]),
                "date_from": st.session_state.get("date_from"),
                "date_to": st.session_state.get("date_to"),
            }
            tool_results_map = {}
            with ThreadPoolExecutor() as executor:
                futures = {executor.submit(execute_tool, b.name, b.input, tool_context): b.id for b in tool_blocks}
                for future in as_completed(futures):
                    tool_results_map[futures[future]] = future.result()

            tool_results = [
                {"type": "tool_result", "tool_use_id": b.id, "content": tool_results_map[b.id]}
                for b in tool_blocks
            ]

            api_messages.append({"role": "assistant", "content": final.content})
            api_messages.append({"role": "user", "content": tool_results})


# --- Streamlit UI ---

def render_md(content: str, placeholder=None):
    converted = re.sub(
        r'\[([^\]]+)\]\((https?://[^\)\s]+)\)',
        lambda m: f'<a href="{m.group(2)}" target="_blank" rel="noopener noreferrer" style="color:#235213;text-decoration:underline">{m.group(1)}</a>',
        content,
    )
    converted = re.sub(
        r'(?<!href=")(https?://[^\s<>()\[\]"]+)',
        lambda m: f'<a href="{m.group(1)}" target="_blank" rel="noopener noreferrer" style="color:#235213;text-decoration:underline">{m.group(1)}</a>',
        converted,
    )
    if placeholder:
        placeholder.markdown(converted, unsafe_allow_html=True)
    else:
        st.markdown(converted, unsafe_allow_html=True)


st.set_page_config(page_title="Poynter Teaching Assistant", layout="wide")

if not ANTHROPIC_API_KEY:
    st.error("ANTHROPIC_API_KEY is not set. Add it to your .env file.")
    st.stop()

if not LD_APP_PASSWORD:
    st.error("LD_APP_PASSWORD is not set. Add it to your .env file.")
    st.stop()

st.markdown(
    '<link href="https://fonts.googleapis.com/css2?family=PT+Serif:ital,wght@0,400;0,700;1,400;1,700&family=Roboto:wght@300;400;500;700&display=swap" rel="stylesheet">',
    unsafe_allow_html=True,
)

_CSS = """
html, body, .stApp {
    background-color: #ffffff !important;
    color: #151515 !important;
    font-family: 'PT Serif', Georgia, serif;
}
[data-testid="stHeader"],
[data-testid="stToolbar"] {
    background-color: #235213 !important;
}
[data-testid="stDecoration"] { display: none; }
.poynter-title {
    position: fixed;
    top: 0;
    left: 50%;
    transform: translateX(-50%);
    z-index: 999999;
    font-family: 'Roboto', sans-serif;
    font-weight: 600;
    font-size: 15px;
    color: #ffffff;
    letter-spacing: 1.5px;
    line-height: 60px;
    pointer-events: none;
    white-space: nowrap;
}
[data-testid="stSidebar"] {
    background-color: #f7f7f7 !important;
    border-right: 1px solid #e4e4e4;
}
[data-testid="stSidebar"] h1,
[data-testid="stSidebar"] h2,
[data-testid="stSidebar"] p,
[data-testid="stSidebar"] label {
    font-family: 'Roboto', sans-serif !important;
    color: #151515 !important;
}
[data-testid="stSidebar"] .stButton button,
[data-testid="stSidebar"] .stButton button:focus,
[data-testid="stSidebar"] .stButton button:active {
    background-color: #235213 !important;
    color: #ffffff !important;
    border: none !important;
    border-radius: 4px !important;
    font-family: 'Roboto', sans-serif;
    font-size: 13px;
    display: flex !important;
    align-items: center !important;
    justify-content: center !important;
    padding: 6px 8px !important;
}
[data-testid="stSidebar"] .stButton button p,
[data-testid="stSidebar"] .stButton button div {
    color: #ffffff !important;
    background: transparent !important;
    font-family: 'Roboto', sans-serif !important;
}
[data-testid="stSidebar"] .stButton button:hover {
    background-color: #1a3d0e !important;
}
.stTabs [data-baseweb="tab-list"] {
    gap: 0;
    border-bottom: 2px solid #e4e4e4;
    background-color: #ffffff !important;
}
.stTabs [data-baseweb="tab"],
.stTabs [data-baseweb="tab"] button,
.stTabs [data-baseweb="tab"] p,
.stTabs [data-testid="stTab"],
.stTabs [data-testid="stTab"] button,
.stTabs [data-testid="stTab"] p {
    font-family: 'Roboto', sans-serif !important;
    font-weight: 500 !important;
    font-size: 15px !important;
    color: #444444 !important;
    background-color: transparent !important;
    padding: 12px 28px;
}
.stTabs [aria-selected="true"],
.stTabs [aria-selected="true"] button,
.stTabs [aria-selected="true"] p {
    color: #235213 !important;
    border-bottom: 3px solid #235213 !important;
    background-color: transparent !important;
}
[data-testid="stChatMessage"] {
    background-color: #ffffff !important;
}
[data-testid="stChatMessage"]:has([data-testid="stChatMessageAvatarUser"]) {
    background-color: #f0f7ee !important;
}
[data-testid="stChatMessage"] .stMarkdown p,
[data-testid="stChatMessage"] .stMarkdown li,
[data-testid="stChatMessage"] .stMarkdown h1,
[data-testid="stChatMessage"] .stMarkdown h2,
[data-testid="stChatMessage"] .stMarkdown h3 {
    font-family: 'PT Serif', Georgia, serif !important;
    color: #151515 !important;
}
[data-testid="stChatMessage"] a,
[data-testid="stChatMessage"] .stMarkdown a {
    color: #235213 !important;
    text-decoration: underline !important;
    pointer-events: auto !important;
    cursor: pointer !important;
}
[data-testid="stChatMessage"] table,
[data-testid="stChatMessage"] th,
[data-testid="stChatMessage"] td,
[data-testid="stChatMessage"] tr {
    color: #151515 !important;
    font-family: 'PT Serif', Georgia, serif !important;
    border-color: #e4e4e4 !important;
}
table, th, td { color: #151515 !important; }
[data-testid="stChatMessageAvatarAssistant"] {
    background-color: #4aa3d2 !important;
}
[data-testid="stChatMessageAvatarUser"] {
    background-color: #ffce3e !important;
}
[data-testid="stChatInputContainer"],
[data-testid="stChatInputContainer"] > div,
[data-testid="stChatInputContainer"] > div > div,
[data-testid="stChatInputContainer"] div[data-baseweb],
[data-testid="stChatInputContainer"] div[class],
[data-testid="stChatInputContainer"] textarea {
    background-color: #ffffff !important;
    color: #151515 !important;
}
[data-testid="stChatInputContainer"] {
    border-top: 3px solid #235213;
    box-shadow: 0 -4px 16px rgba(35,82,19,0.10);
    padding-top: 10px !important;
}
[data-testid="stChatInputContainer"] textarea {
    font-family: 'PT Serif', Georgia, serif !important;
    font-size: 16px !important;
    min-height: 52px !important;
}
[data-testid="stVerticalBlock"]:has(.clear-search-marker) {
    display: flex !important;
    justify-content: flex-end !important;
    padding: 6px 0 4px 0 !important;
}
[data-testid="stVerticalBlock"]:has(.clear-search-marker) [data-testid="stButton"] button {
    background-color: #ffffff !important;
    color: #235213 !important;
    border: 1.5px solid #235213 !important;
    border-radius: 9999px !important;
    font-family: 'Roboto', sans-serif !important;
    font-weight: 600 !important;
    font-size: 12px !important;
    padding: 6px 18px !important;
    transition: all 0.15s ease !important;
}
[data-testid="stVerticalBlock"]:has(.clear-search-marker) [data-testid="stButton"] button:hover {
    background-color: #235213 !important;
    color: #ffffff !important;
}
.stButton > button {
    background-color: #f0f0f0 !important;
    color: #151515 !important;
    border: 1px solid #cccccc !important;
    border-radius: 9999px;
    font-family: 'Roboto', sans-serif;
    font-weight: 500;
    font-size: 13px;
    padding: 8px 20px;
    transition: all 0.15s ease;
}
.stButton > button:hover {
    background-color: #235213 !important;
    color: #ffffff !important;
    border-color: #235213 !important;
}
[data-testid="stDownloadButton"] button {
    background-color: #f0f0f0 !important;
    color: #151515 !important;
    border: 1px solid #cccccc !important;
    border-radius: 9999px;
    font-family: 'Roboto', sans-serif;
    font-weight: 500;
    font-size: 13px;
}
[data-testid="stDownloadButton"] button:hover {
    background-color: #235213 !important;
    color: #ffffff !important;
    border-color: #235213 !important;
}
h1, h2, h3 { font-family: 'PT Serif', Georgia, serif; color: #151515 !important; }
.welcome-text {
    font-family: 'PT Serif', Georgia, serif;
    font-size: 22px;
    color: #151515 !important;
    padding-bottom: 12px;
    border-bottom: 2px solid #235213;
    margin-bottom: 28px;
}
textarea {
    font-family: 'PT Serif', Georgia, serif !important;
    font-size: 15px !important;
    line-height: 1.75 !important;
    color: #151515 !important;
    background-color: #ffffff !important;
    border: 1px solid #e4e4e4 !important;
}
hr { border-color: #e4e4e4; }
[data-testid="stVerticalBlock"]:has(.rm-card) {
    gap: 0 !important;
    margin-top: 8px !important;
    margin-bottom: 6px !important;
}
.rm-card-body {
    background: #ffffff;
    border: 1px solid #e4e4e4;
    border-left-width: 4px;
    border-bottom: none;
    border-radius: 6px 6px 0 0;
    padding: 10px 12px 8px;
    margin: 0;
}
.rm-card-title {
    font-family: 'PT Serif', Georgia, serif;
    font-size: 13px;
    font-weight: 700;
    color: #151515;
    margin: 0 0 4px 0;
    line-height: 1.4;
}
.rm-card-stats {
    font-family: 'Roboto', sans-serif;
    font-size: 11px;
    color: #888888;
    line-height: 1.3;
}
[data-testid="stVerticalBlock"]:has(.rm-card) [data-testid="stHorizontalBlock"] {
    background: #f7f7f7 !important;
    border: 1px solid #e4e4e4 !important;
    border-top: none !important;
    border-radius: 0 0 6px 6px !important;
    padding: 3px 6px !important;
    margin: 0 !important;
}
[data-testid="stVerticalBlock"]:has(.rm-card) [data-testid="stHorizontalBlock"] [data-testid="stButton"] button {
    background: #f0f0f0 !important;
    border: 1px solid #dddddd !important;
    border-radius: 4px !important;
    height: 24px !important;
    min-height: 24px !important;
    overflow: hidden !important;
    padding: 0 8px !important;
    line-height: 24px !important;
    font-size: 11px !important;
    white-space: nowrap !important;
    color: #444444 !important;
    font-family: 'Roboto', sans-serif !important;
    text-align: center !important;
}
[data-testid="stVerticalBlock"]:has(.rm-card) [data-testid="stHorizontalBlock"] [data-testid="stButton"] button:hover {
    background: #235213 !important;
    color: #ffffff !important;
    border-color: #235213 !important;
}
[data-testid="stSegmentedControl"] {
    background: #e8e8e8 !important;
    border-radius: 6px !important;
}
[data-testid="stSegmentedControl"] button {
    background: #e8e8e8 !important;
    color: #444444 !important;
    font-family: 'Roboto', sans-serif !important;
    font-size: 14px !important;
}
[data-testid="stSegmentedControl"] button p {
    color: #444444 !important;
}
[data-testid="stSegmentedControl"] button[aria-checked="true"] {
    background: #ffffff !important;
    color: #235213 !important;
    font-weight: 600 !important;
}
[data-testid="stSegmentedControl"] button[aria-checked="true"] p {
    color: #235213 !important;
}
[data-testid="stHeader"] svg,
[data-testid="stHeader"] svg * {
    fill: #ffffff !important;
    stroke: #ffffff !important;
}
[data-testid="stBaseButton-headerNoPadding"] {
    background: transparent !important;
    background-color: transparent !important;
    border: none !important;
    box-shadow: none !important;
    color: #ffffff !important;
}
[data-testid="stBaseButton-headerNoPadding"] svg,
[data-testid="stBaseButton-headerNoPadding"] svg * {
    fill: #ffffff !important;
    stroke: #ffffff !important;
}
[data-testid="stSidebarCollapseButton"] button,
[data-testid="stSidebarCollapsedControl"] button {
    background: transparent !important;
    background-color: transparent !important;
    border: none !important;
    box-shadow: none !important;
}
[data-testid="stSidebarCollapseButton"] [data-testid="stIconMaterial"],
[data-testid="stSidebarCollapsedControl"] [data-testid="stIconMaterial"],
[data-testid="stHeader"] [data-testid="stIconMaterial"] {
    color: #ffffff !important;
}
[data-testid="stVerticalBlock"]:has(.filter-bar-marker) {
    background-color: #f7f7f7 !important;
    border: 1px solid #e4e4e4 !important;
    border-radius: 6px !important;
    padding: 14px 18px 10px !important;
    margin-bottom: 14px !important;
}
.filter-heading {
    font-family: 'Roboto', sans-serif !important;
    font-size: 10px !important;
    font-weight: 700 !important;
    letter-spacing: 1.2px !important;
    text-transform: uppercase !important;
    color: #888888 !important;
    margin-bottom: 6px !important;
    margin-top: 0 !important;
}
.filter-bar label {
    font-family: 'Roboto', sans-serif !important;
    font-size: 13px !important;
    color: #333333 !important;
}
[data-testid="stCheckbox"] p,
[data-testid="stCheckbox"] label,
[data-testid="stCheckbox"] span:not([data-baseweb]) {
    color: #151515 !important;
    font-family: 'Roboto', sans-serif !important;
    font-size: 13px !important;
}
[data-baseweb="checkbox"] span {
    border-color: #235213 !important;
}
[data-baseweb="checkbox"] [data-checked="true"] {
    background-color: #235213 !important;
}
[data-testid="stVerticalBlock"]:has(.sort-hdr) [data-testid="stButton"] button {
    background: transparent !important;
    border: none !important;
    border-radius: 0 !important;
    box-shadow: none !important;
    font-family: 'Roboto', sans-serif !important;
    font-size: 11px !important;
    font-weight: 700 !important;
    letter-spacing: 1px !important;
    text-transform: uppercase !important;
    color: #888888 !important;
    padding: 2px 0 !important;
    height: auto !important;
    min-height: 0 !important;
    text-align: left !important;
    cursor: pointer !important;
}
[data-testid="stVerticalBlock"]:has(.sort-hdr) [data-testid="stButton"] button:hover {
    background: transparent !important;
    color: #235213 !important;
    border: none !important;
    box-shadow: none !important;
}
[data-testid="stVerticalBlock"]:has(.sort-hdr) [data-testid="stButton"] button p {
    font-family: 'Roboto', sans-serif !important;
    font-size: 11px !important;
    font-weight: 700 !important;
    letter-spacing: 1px !important;
    text-transform: uppercase !important;
    color: inherit !important;
}
.date-sublabel {
    font-family: 'Roboto', sans-serif !important;
    font-size: 11px !important;
    color: #888888 !important;
    margin: 0 0 2px 0 !important;
}
"""

st.markdown(f"<style>{_CSS}</style>", unsafe_allow_html=True)
st.markdown(
    '<div class="poynter-title">POYNTER &nbsp;&middot;&nbsp; Teaching Assistant</div>',
    unsafe_allow_html=True,
)

# --- Auth ---
if "code" in st.query_params and "oauth_code" not in st.session_state:
    st.session_state["oauth_code"] = st.query_params["code"]
    st.session_state["oauth_state"] = st.query_params.get("state", "")
    st.query_params.clear()
    st.rerun()

if "oauth_code" in st.session_state:
    _code = st.session_state.pop("oauth_code")
    _state = st.session_state.pop("oauth_state", "")
    try:
        _info = fetch_google_user_info(_code, _state)
        st.session_state["auth_email"] = _info["email"]
        st.session_state["google_access_token"] = _info["access_token"]
        st.session_state["google_refresh_token"] = _info["refresh_token"]
        st.rerun()
    except Exception as _e:
        st.error(f"Sign-in failed: {_e}")
        st.stop()

_auth_email = st.session_state.get("auth_email", "")

if not _auth_email:
    st.markdown(
        '<div style="max-width:400px;margin:100px auto;text-align:center">'
        '<p style="font-family:Roboto,sans-serif;font-size:15px;color:#555;margin-bottom:28px">'
        'Sign in with your Poynter Google account to continue.</p>'
        f'<a href="{build_google_auth_url()}" target="_self" '
        'style="display:inline-block;background:#235213;color:#fff;font-family:Roboto,sans-serif;'
        'font-size:14px;font-weight:600;padding:12px 28px;border-radius:6px;text-decoration:none">'
        'Sign in with Google</a>'
        '</div>',
        unsafe_allow_html=True,
    )
    st.stop()

if not _auth_email.endswith("@poynter.org"):
    st.error("Access is restricted to Poynter staff. Please sign in with your @poynter.org account.")
    if st.button("Sign out"):
        st.session_state.pop("auth_email", None)
        st.rerun()
    st.stop()

_user_email = _auth_email
st.session_state["user_email"] = _user_email

# --- Session state ---
for key, default in [
    ("explore_messages", []),
    ("dev_messages", []),
    ("active_doc", None),
    ("renaming_doc", None),
    ("src_lms", True),
    ("src_drive", True),
    ("drive_types_select", ["Docs", "Slides", "Sheets"]),
    ("date_from", None),
    ("date_to", None),
    ("auditor_list_page", 0),
    ("auditor_sort_col", "Course"),
    ("auditor_sort_dir", "asc"),
]:
    if key not in st.session_state:
        st.session_state[key] = default

if "documents" not in st.session_state:
    st.session_state.documents = load_documents()

if "auditor_results" not in st.session_state:
    st.session_state.auditor_results = load_auditor_results()

if "_storage_error" in st.session_state:
    st.warning(f"Storage error (reload to retry): {st.session_state.pop('_storage_error')}")


def save_as_document(content: str):
    n = len(st.session_state.documents) + 1
    doc = {"id": str(uuid.uuid4()), "name": f"Document {n}", "content": content}
    st.session_state.documents.append(doc)
    st.session_state.active_doc = len(st.session_state.documents) - 1
    persist_documents()


# --- Sidebar ---
with st.sidebar:
    st.caption(f"Signed in as {_user_email}")
    if st.button("Sign out", key="signout"):
        for _k in ("auth_email", "google_access_token", "google_refresh_token"):
            st.session_state.pop(_k, None)
        st.rerun()
    _drive_token = st.session_state.get("google_access_token", "")
    if not DRIVE_AVAILABLE:
        st.caption("Drive: unavailable (import error)")
    elif _drive_token:
        st.caption("Drive: connected")
    else:
        st.caption("Drive: not connected — sign out and back in")
    st.divider()
    if st.session_state.active_doc is not None:
        doc = st.session_state.documents[st.session_state.active_doc]
        c1, c2 = st.columns([3, 1])
        c1.subheader(doc["name"])
        if c2.button("✕ Close", use_container_width=True):
            st.session_state.active_doc = None
            st.rerun()

        edited = st.text_area(
            "content",
            value=doc["content"],
            height=500,
            label_visibility="collapsed",
            key=f"doc_editor_{st.session_state.active_doc}",
        )
        st.session_state.documents[st.session_state.active_doc]["content"] = edited

        c1, c2 = st.columns(2)
        if c1.button("Save", use_container_width=True):
            persist_documents()
            st.toast("Document saved.")
        c2.download_button(
            "Download",
            data=doc["content"],
            file_name=f"{doc['name']}.md",
            mime="text/markdown",
            use_container_width=True,
        )
    else:
        st.title("Documents")
        if not st.session_state.documents:
            st.caption("No documents yet. Save an AI response to get started.")
        else:
            for i, doc in enumerate(st.session_state.documents):
                if st.session_state.renaming_doc == i:
                    new_name = st.text_input("Rename", value=doc["name"], key=f"rename_input_{i}")
                    c1, c2 = st.columns(2)
                    if c1.button("Save", key=f"rename_save_{i}"):
                        st.session_state.documents[i]["name"] = new_name
                        st.session_state.renaming_doc = None
                        persist_documents()
                        st.rerun()
                    if c2.button("Cancel", key=f"rename_cancel_{i}"):
                        st.session_state.renaming_doc = None
                        st.rerun()
                else:
                    st.markdown(doc["name"])
                    c1, c2, c3 = st.columns([3, 1, 1])
                    if c1.button("Open", key=f"open_{i}", use_container_width=True):
                        st.session_state.active_doc = i
                        st.rerun()
                    if c2.button("✏️", key=f"rename_{i}", use_container_width=True, help="Rename"):
                        st.session_state.renaming_doc = i
                        st.rerun()
                    if c3.button("🗑️", key=f"delete_{i}", use_container_width=True, help="Delete"):
                        st.session_state.documents.pop(i)
                        if st.session_state.active_doc == i:
                            st.session_state.active_doc = None
                        elif st.session_state.active_doc and st.session_state.active_doc > i:
                            st.session_state.active_doc -= 1
                        persist_documents()
                        st.rerun()


# --- Filter bar ---
with st.container():
    st.markdown('<span class="filter-bar-marker"></span>', unsafe_allow_html=True)
    fc1, fc3, fc4 = st.columns([1.4, 2.8, 2.2])

    with fc1:
        st.markdown('<p class="filter-heading">Search</p>', unsafe_allow_html=True)
        st.checkbox("Poynter LMS", key="src_lms")
        st.checkbox("Google Drive", key="src_drive")

    with fc3:
        st.markdown('<p class="filter-heading">Drive File Types</p>', unsafe_allow_html=True)
        st.multiselect(
            "drive_types_label",
            options=["Docs", "Slides", "Sheets", "Forms", "Images"],
            key="drive_types_select",
            disabled=not st.session_state.src_drive,
            label_visibility="collapsed",
        )

    with fc4:
        st.markdown('<p class="filter-heading">Date Range</p>', unsafe_allow_html=True)
        dcol1, dcol2 = st.columns(2)
        with dcol1:
            st.markdown('<p class="date-sublabel">From</p>', unsafe_allow_html=True)
            st.date_input("From", value=None, key="date_from", label_visibility="collapsed")
        with dcol2:
            st.markdown('<p class="date-sublabel">To</p>', unsafe_allow_html=True)
            st.date_input("To", value=None, key="date_to", label_visibility="collapsed")


# --- Shared chat renderer ---

@st.dialog("Start a new search?")
def confirm_new_search(messages_key: str):
    st.warning(
        "Unsaved responses will be lost. Make sure you've saved anything you want to keep before continuing.",
        icon="⚠️",
    )
    col1, col2 = st.columns(2)
    if col1.button("Clear and start over", type="primary", use_container_width=True):
        st.session_state[messages_key] = []
        st.rerun()
    if col2.button("Cancel", use_container_width=True):
        st.rerun()


def render_chat(messages_key: str, welcome: str, placeholder_text: str):
    messages = st.session_state[messages_key]
    active_tools = get_active_tools()
    system_addendum = build_filter_system_addendum()

    if not messages:
        st.markdown(f'<div class="welcome-text">{welcome}</div>', unsafe_allow_html=True)

    for i, msg in enumerate(messages):
        with st.chat_message(msg["role"]):
            render_md(msg["content"])
        if msg["role"] == "assistant":
            with st.expander("Debug: raw response", expanded=False):
                st.code(msg["content"], language=None)
            if st.button("Save as Doc", key=f"{messages_key}_save_{i}"):
                save_as_document(msg["content"])
                st.rerun()

    if prompt := st.chat_input(placeholder_text):
        messages.append({"role": "user", "content": prompt})
        with st.chat_message("user"):
            render_md(prompt)
        with st.chat_message("assistant"):
            reply_placeholder = st.empty()
            reply = run_claude_streaming(messages, reply_placeholder, active_tools, system_addendum)
            render_md(reply, reply_placeholder)
        messages.append({"role": "assistant", "content": reply})
        st.rerun()

    with st.container():
        st.markdown('<span class="clear-search-marker"></span>', unsafe_allow_html=True)
        if st.button("Clear Search", key=f"{messages_key}_new_search"):
            confirm_new_search(messages_key)


# --- Course Auditor UI ---

def generate_report_html(course_id: str) -> str:
    result = st.session_state.auditor_results.get(course_id, {})
    courses = fetch_all_courses_cached()
    course = next((c for c in courses if c["id"] == course_id), {})

    title = result.get("title", "Course Report")
    link = course.get("link", "")
    lift = result.get("lift", "—")
    relevance = result.get("relevance", "—")
    issues = result.get("issues", [])
    priority = result.get("quarter", "—")
    level = result.get("level", "—")
    summary = result.get("summary", "")
    today = date.today().strftime("%B %d, %Y")

    p_colors = {"High Priority": "#235213", "Medium Priority": "#b45309", "Low Priority": "#b91c1c"}
    l_colors = {"High": "#dc2626", "Medium": "#d97706", "Low": "#235213"}
    pc = p_colors.get(priority, "#444444")
    lc = l_colors.get(level, "#444444")

    issue_icons = {"Outdated": "🟠", "Missing_AI": "🤖", "Broken_Link": "🔗", "Quality": "📝"}
    findings_html = ""
    if issues:
        for issue in issues:
            icon = issue_icons.get(issue.get("type", ""), "•")
            findings_html += (
                f'<div class="finding">'
                f'<div class="fh">{icon} <strong>{issue.get("type","")}</strong>'
                f' &mdash; <em>{issue.get("loc","")}</em></div>'
                f'<div class="fd">{issue.get("desc","")}</div>'
                f'</div>'
            )
    else:
        findings_html = '<div class="no-issues">✓ No issues identified.</div>'

    link_html = (
        f'<a class="course-link" href="{link}">View on Poynter.org ↗</a>' if link else ""
    )
    summary_html = (
        f'<div class="sh">Summary</div><div class="summary">{summary}</div>' if summary else ""
    )

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Audit: {title}</title>
<link href="https://fonts.googleapis.com/css2?family=PT+Serif:ital,wght@0,400;0,700;1,400&family=Roboto:wght@300;400;500;700&display=swap" rel="stylesheet">
<style>
*{{box-sizing:border-box;margin:0;padding:0}}
body{{font-family:'PT Serif',Georgia,serif;color:#151515;background:#f2f2f2;padding:40px 20px}}
.page{{max-width:760px;margin:0 auto;background:#fff;border-radius:8px;box-shadow:0 2px 16px rgba(0,0,0,.09);overflow:hidden}}
.hd{{background:#235213;padding:28px 32px;color:#fff}}
.hd .org{{font-family:'Roboto',sans-serif;font-size:10px;font-weight:700;letter-spacing:2px;text-transform:uppercase;opacity:.7;margin-bottom:10px}}
.hd h1{{font-size:22px;font-weight:700;line-height:1.3;margin-bottom:8px}}
.course-link{{font-family:'Roboto',sans-serif;font-size:12px;color:rgba(255,255,255,.75);text-decoration:none;border-bottom:1px solid rgba(255,255,255,.35)}}
.hd .dt{{font-family:'Roboto',sans-serif;font-size:11px;color:rgba(255,255,255,.5);margin-top:10px}}
.bd{{padding:32px}}
.metrics{{display:flex;gap:14px;margin-bottom:22px}}
.metric{{flex:1;background:#f7f7f7;border:1px solid #e4e4e4;border-radius:6px;padding:16px;text-align:center}}
.ml{{font-family:'Roboto',sans-serif;font-size:10px;font-weight:700;letter-spacing:1.2px;text-transform:uppercase;color:#888;margin-bottom:6px}}
.mv{{font-size:30px;font-weight:700;color:#151515}}
.badges{{display:flex;gap:8px;margin-bottom:24px;flex-wrap:wrap}}
.badge{{font-family:'Roboto',sans-serif;font-size:10px;font-weight:700;letter-spacing:1px;text-transform:uppercase;padding:4px 10px;border-radius:4px}}
.bp{{background:{pc}18;color:{pc};border:1px solid {pc}44}}
.bl{{background:{lc}18;color:{lc};border:1px solid {lc}44}}
.sh{{font-family:'Roboto',sans-serif;font-size:10px;font-weight:700;letter-spacing:1.5px;text-transform:uppercase;color:#888;margin-bottom:10px;padding-bottom:6px;border-bottom:1px solid #e4e4e4}}
.summary{{background:#f0f7ee;border-left:4px solid #235213;border-radius:0 6px 6px 0;padding:14px 16px;margin-bottom:26px;font-size:14px;line-height:1.75;color:#1a3d0e}}
.findings{{margin-bottom:16px}}
.finding{{border:1px solid #e4e4e4;border-radius:6px;padding:14px 16px;margin-bottom:10px}}
.fh{{font-family:'Roboto',sans-serif;font-size:13px;margin-bottom:6px}}
.fd{{font-size:13px;color:#444;line-height:1.65}}
.no-issues{{background:#f0f7ee;border:1px solid #b8d9a8;border-radius:6px;padding:14px 16px;font-family:'Roboto',sans-serif;font-size:13px;color:#235213}}
.ft{{margin-top:28px;padding-top:14px;border-top:1px solid #e4e4e4;font-family:'Roboto',sans-serif;font-size:11px;color:#bbb;text-align:center}}
</style>
</head>
<body>
<div class="page">
  <div class="hd">
    <div class="org">Poynter Institute &middot; Course Audit Report</div>
    <h1>{title}</h1>
    {link_html}
    <div class="dt">Generated {today}</div>
  </div>
  <div class="bd">
    <div class="metrics">
      <div class="metric"><div class="ml">Lift Score</div><div class="mv">{lift}</div></div>
      <div class="metric"><div class="ml">Relevance</div><div class="mv">{relevance}</div></div>
      <div class="metric"><div class="ml">Issues Found</div><div class="mv">{len(issues)}</div></div>
    </div>
    <div class="badges">
      <span class="badge bp">{priority}</span>
      <span class="badge bl">Lift Level: {level}</span>
    </div>
    {summary_html}
    <div class="sh">Findings</div>
    <div class="findings">{findings_html}</div>
    <div class="ft">Poynter Institute &middot; Course Audit &middot; {today}</div>
  </div>
</div>
</body>
</html>"""


@st.dialog("Course Audit Report", width="large")
def show_audit_report_dialog(course_id: str):
    result = st.session_state.auditor_results.get(course_id, {})
    courses = fetch_all_courses_cached()
    course = next((c for c in courses if c["id"] == course_id), {})

    title = result.get("title", "Course Report")
    st.markdown(f"### {title}")

    dl_col, link_col = st.columns([1, 3])
    safe_name = re.sub(r"[^\w\s-]", "", title).strip().replace(" ", "_")[:50]
    dl_col.download_button(
        "Download report",
        data=generate_report_html(course_id),
        file_name=f"audit_{safe_name}.html",
        mime="text/html",
        key=f"dl_{course_id}",
    )
    if course.get("link"):
        link_col.markdown(f"[View on Poynter.org ↗]({course['link']})")

    c1, c2, c3 = st.columns(3)
    c1.metric("Lift Score", result.get("lift", "—"))
    c2.metric("Relevance", result.get("relevance", "—"))
    c3.metric("Issues Found", len(result.get("issues", [])))

    st.markdown(
        f"**Priority:** {result.get('quarter','—')} &nbsp;·&nbsp; "
        f"**Level:** {result.get('level','—')}"
    )

    if result.get("summary"):
        st.info(result["summary"])

    issues = result.get("issues", [])
    if issues:
        st.markdown("#### Findings")
        icons = {"Outdated": "🟠", "Missing_AI": "🤖", "Broken_Link": "🔗", "Quality": "📝"}
        for issue in issues:
            icon = icons.get(issue.get("type", ""), "•")
            st.markdown(
                f"**{icon} {issue.get('type','')}** — *{issue.get('loc','')}*  \n{issue.get('desc','')}"
            )
            st.divider()
    else:
        st.success("No issues identified.")

    if st.button("Re-audit this course", key=f"reaudit_{course_id}"):
        with st.spinner("Re-auditing…"):
            new_result = run_course_audit(course_id, result.get("title", ""))
            st.session_state.auditor_results[course_id] = new_result
            persist_auditor_results()
        st.rerun()


def render_auditor():
    results = st.session_state.auditor_results
    audited = list(results.values())

    nav = st.segmented_control(
        "Auditor navigation",
        options=["Course List", "Roadmap"],
        key="auditor_nav",
        label_visibility="collapsed",
    )
    page = nav or "Course List"

    st.markdown('<hr style="margin:8px 0 20px 0;border-color:#e4e4e4;">', unsafe_allow_html=True)

    # ── Course List ───────────────────────────────────────
    if page == "Course List":
        courses = fetch_all_courses_cached()
        if not courses:
            err = st.session_state.get("ld_fetch_error", "Unknown error")
            st.error(f"Could not load courses from LearnDash: {err}")
            return

        s1, s2 = st.columns([3, 1.5])
        search = s1.text_input(
            "Search", placeholder="Filter by course title…",
            label_visibility="collapsed", key="auditor_search_box",
        )
        filter_opt = s2.selectbox(
            "Filter", ["All", "Audited", "Not Audited", "High Lift", "Missing AI"],
            label_visibility="collapsed", key="auditor_filter_opt",
        )

        filtered = courses
        if search:
            ql = search.lower()
            filtered = [c for c in filtered if ql in c["title"].lower()]
        if filter_opt == "Audited":
            filtered = [c for c in filtered if c["id"] in results]
        elif filter_opt == "Not Audited":
            filtered = [c for c in filtered if c["id"] not in results]
        elif filter_opt == "High Lift":
            filtered = [c for c in filtered if results.get(c["id"], {}).get("level") == "High"]
        elif filter_opt == "Missing AI":
            filtered = [c for c in filtered if any(
                i.get("type") == "Missing_AI"
                for i in results.get(c["id"], {}).get("issues", [])
            )]

        # Sort
        sort_col = st.session_state.get("auditor_sort_col", "Course")
        sort_dir = st.session_state.get("auditor_sort_dir", "asc")
        reverse = sort_dir == "desc"
        if sort_col == "Course":
            filtered = sorted(filtered, key=lambda c: c["title"].lower(), reverse=reverse)
        elif sort_col == "Modified":
            filtered = sorted(filtered, key=lambda c: c.get("modified", ""), reverse=reverse)
        elif sort_col == "Lift":
            sentinel = -1 if reverse else 999
            filtered = sorted(filtered, key=lambda c: results.get(c["id"], {}).get("lift", sentinel), reverse=reverse)

        total = len(filtered)
        total_pages = max(1, (total + AUDIT_PAGE_SIZE - 1) // AUDIT_PAGE_SIZE)
        page_num = min(st.session_state.get("auditor_list_page", 0), total_pages - 1)
        page_courses = filtered[page_num * AUDIT_PAGE_SIZE:(page_num + 1) * AUDIT_PAGE_SIZE]

        st.caption(f"{total} course{'s' if total != 1 else ''} · Page {page_num + 1} of {total_pages}")

        # Column headers (clickable for sort)
        with st.container():
            st.markdown('<span class="sort-hdr"></span>', unsafe_allow_html=True)
            hc1, hc2, hc3, hc4, hc5 = st.columns([4, 1.5, 1.5, 1, 1])
            for col, label in zip([hc1, hc2, hc3], ["Course", "Modified", "Lift"]):
                arrow = (" ▲" if sort_dir == "asc" else " ▼") if sort_col == label else ""
                if col.button(label + arrow, key=f"sort_{label}", use_container_width=True):
                    if sort_col == label:
                        st.session_state.auditor_sort_dir = "desc" if sort_dir == "asc" else "asc"
                    else:
                        st.session_state.auditor_sort_col = label
                        st.session_state.auditor_sort_dir = "asc"
                    st.session_state.auditor_list_page = 0
                    st.rerun()
        st.markdown('<div style="border-bottom:1px solid #e4e4e4;margin:2px 0 6px 0;"></div>', unsafe_allow_html=True)

        level_icons = {"High": "🔴", "Medium": "🟡", "Low": "🟢"}

        for course in page_courses:
            cid = course["id"]
            result = results.get(cid)
            rc1, rc2, rc3, rc4, rc5 = st.columns([4, 1.5, 1.5, 1, 1])

            rc1.markdown(f"**{course['title']}**" if result else course["title"])
            rc2.caption(course.get("modified", "—"))

            if result:
                level = result.get("level", "")
                rc3.markdown(f"{level_icons.get(level, '')} {level}")
            else:
                rc3.caption("—")

            if result:
                if rc4.button("View", key=f"view_{cid}"):
                    show_audit_report_dialog(cid)

            audit_label = "Audit" if not result else "↺"
            if rc5.button(audit_label, key=f"audit_{cid}"):
                with st.spinner(f"Auditing \"{course['title'][:35]}...\""):
                    audit_result = run_course_audit(cid, course["title"])
                    st.session_state.auditor_results[cid] = audit_result
                    persist_auditor_results()
                st.rerun()

            st.markdown('<div style="border-bottom:1px solid #f0f0f0;margin:2px 0;"></div>', unsafe_allow_html=True)

        # Pagination
        pg1, pg2, pg3 = st.columns([1, 4, 1])
        if pg1.button("← Prev", disabled=page_num == 0, key="audit_prev_pg"):
            st.session_state.auditor_list_page = page_num - 1
            st.rerun()
        pg2.markdown(
            f'<div style="text-align:center;padding:8px 0;color:#888;font-size:13px;'
            f'font-family:Roboto,sans-serif">{page_num + 1} / {total_pages}</div>',
            unsafe_allow_html=True,
        )
        if pg3.button("Next →", disabled=page_num >= total_pages - 1, key="audit_next_pg"):
            st.session_state.auditor_list_page = page_num + 1
            st.rerun()

    # ── Roadmap ───────────────────────────────────────────
    else:
        if not audited:
            st.info("No audited courses yet. Go to **Course List** to audit courses.")
            return

        # Migrate legacy Q1/Q2/Backlog values
        legacy_map = {"Q1": "High Priority", "Q2": "Medium Priority", "Backlog": "Low Priority"}
        needs_save = False
        for r in st.session_state.auditor_results.values():
            if r.get("quarter") in legacy_map:
                r["quarter"] = legacy_map[r["quarter"]]
                needs_save = True
        if needs_save:
            persist_auditor_results()

        PRIORITIES = ["High Priority", "Medium Priority", "Low Priority"]
        buckets = {p: [] for p in PRIORITIES}
        for r in audited:
            q = r.get("quarter", "Low Priority")
            if q not in buckets:
                q = "Low Priority"
            buckets[q].append(r)

        q_styles = {
            "High Priority":   {"bg": "#f0f7ee", "color": "#235213", "border": "#235213"},
            "Medium Priority": {"bg": "#fff8ee", "color": "#b45309", "border": "#d97706"},
            "Low Priority":    {"bg": "#fff5f5", "color": "#b91c1c", "border": "#dc2626"},
        }

        cols = st.columns(3)
        for col, p in zip(cols, PRIORITIES):
            s = q_styles[p]
            items_in_bucket = sorted(buckets[p], key=lambda x: -x.get("relevance", 0))
            with col:
                st.markdown(
                    f'<div style="background:{s["bg"]};color:{s["color"]};border-left:4px solid {s["border"]};'
                    f'padding:8px 12px;border-radius:6px 6px 0 0;'
                    f'font-family:Roboto,sans-serif;font-weight:700;font-size:11px;letter-spacing:1.2px;'
                    f'text-transform:uppercase;margin-bottom:0">{p} &nbsp;·&nbsp; {len(items_in_bucket)}</div>',
                    unsafe_allow_html=True,
                )
                if not items_in_bucket:
                    st.markdown(
                        '<div style="padding:20px;color:#999;font-size:13px;text-align:center;'
                        'font-family:Roboto,sans-serif">No courses</div>',
                        unsafe_allow_html=True,
                    )
                else:
                    p_idx = PRIORITIES.index(p)
                    cc = {"High Priority": "rm-hp", "Medium Priority": "rm-mp", "Low Priority": "rm-lp"}[p]
                    bc = {"rm-hp": "#235213", "rm-mp": "#d97706", "rm-lp": "#dc2626"}[cc]
                    for r in items_in_bucket:
                        cid = r.get("course_id", "")
                        issues_n = len(r.get("issues", []))
                        stats = f"Lift {r.get('lift','?')} · Rel {r.get('relevance','?')} · {issues_n} issue{'s' if issues_n != 1 else ''}"
                        with st.container():
                            st.markdown(f'<span class="rm-card {cc}"></span>', unsafe_allow_html=True)
                            st.markdown(
                                f'<div class="rm-card-body" style="border-left-color:{bc}">'
                                f'<div class="rm-card-title">{r["title"]}</div>'
                                f'<div class="rm-card-stats">{stats}</div>'
                                f'</div>',
                                unsafe_allow_html=True,
                            )
                            ac1, ac2, ac3 = st.columns([3, 1, 1])
                            if ac1.button("View report", key=f"card_{cid}", use_container_width=True):
                                show_audit_report_dialog(cid)
                            if p_idx > 0 and ac2.button("↑", key=f"up_{cid}", use_container_width=True, help="Higher priority"):
                                st.session_state.auditor_results[cid]["quarter"] = PRIORITIES[p_idx - 1]
                                persist_auditor_results()
                                st.rerun()
                            if p_idx < 2 and ac3.button("↓", key=f"dn_{cid}", use_container_width=True, help="Lower priority"):
                                st.session_state.auditor_results[cid]["quarter"] = PRIORITIES[p_idx + 1]
                                persist_auditor_results()
                                st.rerun()


# --- Tabs ---

tab1, tab2, tab3 = st.tabs(["Explore Content", "Course Development", "Course Auditor"])

with tab1:
    render_chat(
        messages_key="explore_messages",
        welcome="What content are you looking for today?",
        placeholder_text="What content are you looking for today?",
    )

with tab2:
    render_chat(
        messages_key="dev_messages",
        welcome="What are we working on today?",
        placeholder_text="What are we working on today?",
    )

with tab3:
    render_auditor()
