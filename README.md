# Poynter Teaching Assistant

An AI chat assistant for Poynter's teaching team. Search 8,000+ LearnDash courses and Google Drive from a single interface, powered by Claude.

## What it does

- **Explore Content** — find lessons, topics, and courses on any subject using semantic search (meaning-based, not just keyword matching)
- **Course Development** — a separate chat context for working on new or existing course material
- **Google Drive search** — searches team Drive files in parallel with the course catalog
- **Direct links** — every result includes a clickable link back to the source
- **Save to Documents** — save any AI response to the built-in document editor, rename it, and download as Markdown

## Setup

### 1. Clone the repo

```bash
git clone <repo-url>
cd poynter-chat-v2
```

### 2. Install dependencies

```bash
pip install -r requirements.txt
```

### 3. Configure environment variables

Create a `.env` file in the project root:

```
ANTHROPIC_API_KEY=your_key_here
LD_USERNAME=ld_api_read
LD_APP_PASSWORD=your_learndash_password_here
```

### 4. Set up Google Drive (optional)

If you want Drive search to work, you'll need a Google OAuth credentials file. Place `credentials.json` in the project root, then run:

```bash
python authenticate_drive.py
```

This will open a browser window to authorize access and save a `token.json` file locally.

> If you skip this step, the app still works — Drive search will just be unavailable.

### 5. Run the app

```bash
streamlit run app.py
```

The app opens at `http://localhost:8501`.

## Files to keep out of git

Make sure your `.gitignore` includes:

```
.env
token.json
credentials.json
documents.json
```

## Tech stack

| Component | Library |
|---|---|
| UI | Streamlit |
| AI | Claude via Anthropic API |
| Semantic search | Qdrant + sentence-transformers |
| LMS data | LearnDash REST API |
| Drive search | Google Drive API v3 |

## Requirements

- Python 3.9+
- An [Anthropic API key](https://console.anthropic.com/)
- LearnDash API credentials (read-only)
- (Optional) Google Cloud project with Drive API enabled
