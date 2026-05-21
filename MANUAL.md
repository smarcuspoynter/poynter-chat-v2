# Poynter Teaching Assistant — User Guide

## What This Tool Does

The Poynter Teaching Assistant is an AI-powered research and planning tool for Poynter's teaching team. It connects directly to two content sources — the Poynter LearnDash LMS and your Google Drive — and lets you search, analyze, and evaluate content using natural language.

**Core capabilities:**
- Find existing LMS courses, lessons, and topics by topic, keyword, or concept
- Search Google Drive documents, slide decks, and sheets alongside LMS content
- Read and analyze the actual content of what it finds, not just titles
- Audit courses for freshness, relevance, and AI-readiness
- Save AI responses as personal documents for later reference

---

## Signing In

Go to [poynter-chat.streamlit.app](https://poynter-chat.streamlit.app) and click **Sign in with Google**. Use your @poynter.org account. Sign-in opens in a new tab — after authenticating with Google, the app will load in that tab with your session active.

The sidebar will show your email and whether Google Drive is connected. If Drive shows "not connected," sign out and back in.

---

## The Sidebar

The sidebar is visible on every tab and has two functions:

**Account:** Shows your signed-in email and a Sign out button. Signing out clears your session — you'll need to sign back in to continue.

**Documents:** Your personal document library. Any AI response can be saved here for future reference. Documents are stored to your account only — other users cannot see them.

- **Save:** Click "Save as Doc" below any AI response to add it to your library
- **Open:** Opens the document in the sidebar for reading and editing
- **Rename:** Click the pencil icon to rename a document
- **Download:** Downloads the document as a `.md` (Markdown) file
- **Delete:** Removes the document permanently

---

## The Filter Bar

The filter bar sits above all three tabs and controls what sources are searched.

**Search sources:**
- **Poynter LMS** — searches the full LearnDash course catalog using both semantic (meaning-based) and keyword search
- **Google Drive** — searches your Drive and shared files for documents, slide decks, and sheets

Both are on by default. You can turn either off to limit results to one source.

**Drive File Types:** When Drive is enabled, you can restrict results to specific file types: Docs, Slides, Sheets, Forms, or Images. All are included by default.

**Date Range:** Narrows Drive results to files modified within a specific date range. Useful when looking for recently updated materials.

---

## Tab 1: Explore Content

This is the primary research tab. Use it to find what Poynter has on any topic — across both the LMS and Drive — and to analyze and evaluate that content.

The assistant searches both sources automatically and reads the actual text of what it finds before responding.

### Best Practices

- **Be specific about what you want:** "Find everything on reverse image search" returns broader results than "Find lessons that teach students how to verify images on social media."
- **Ask follow-up questions:** The assistant remembers the conversation. After getting results, ask it to compare, evaluate, or drill deeper.
- **Ask for evaluation, not just results:** The assistant can read full lesson content and give you a critical analysis, not just a list of links.
- **Let it check both sources together:** For consistency analysis, having both LMS and Drive enabled gives the most complete picture of what exists.

### Suggested Prompts

**Finding content:**
- "What do we have on reverse image search across the LMS and Drive?"
- "Find all lessons that mention misinformation on social media"
- "Show me everything we teach about source evaluation"
- "Do we have any slide decks on media literacy for teens?"

**Analyzing content:**
- "Search for everything we have on AI-generated images and evaluate whether our methods are consistent across courses"
- "Find the lesson on [topic] and tell me whether it feels current or stale"
- "Compare how reverse image search is taught in our different courses — are we saying the same things?"

**Navigating the catalog:**
- "List all courses related to fact-checking"
- "Show me the full structure of the MediaWise course"
- "What topics are covered in the [course name] course?"

---

## Tab 2: Course Development

A general-purpose AI workspace for course development tasks. Use this tab when you're actively building or revising a course rather than exploring existing content.

The same LMS and Drive search tools are available here. The distinction from Explore Content is intent — this tab is for generative, iterative work.

### Best Practices

- Paste in draft content and ask for feedback, restructuring suggestions, or gap analysis
- Use it to draft learning objectives, outlines, or assessment questions
- Ask it to compare your draft against existing LMS content to avoid redundancy

### Suggested Prompts

- "Here's my draft outline for a lesson on deepfakes — what's missing?"
- "Write three learning objectives for a module on evaluating AI-generated images"
- "I'm building a course on news literacy for high schoolers. What does our existing catalog already cover that I should know about before I start?"
- "Review this lesson draft for clarity and suggest edits: [paste content]"

---

## Tab 3: Course Auditor

The Course Auditor evaluates courses in the LMS for update priority. It reads each course's full content and scores it across three dimensions:

- **Lift:** How much work is needed to update it (High / Medium / Low)
- **Relevance:** How relevant the course content still is today
- **Issues:** Specific problems found — outdated statistics, missing AI coverage, broken flows, etc.

The auditor has two views:

### Course List

Displays all courses in the catalog. For each course you can:

- **Audit:** Runs the AI audit on that course. Takes about 30–60 seconds.
- **View:** Opens the full audit report for any previously audited course
- **Re-audit:** The ↺ button re-runs the audit if you want a fresh assessment

**Filtering and sorting:**
- Filter by All, Audited, Not Audited, High Lift, or Missing AI
- Sort by Course name, Last Modified date, or Lift score
- Search by title to find a specific course quickly

Audit results are shared across all users — if a colleague audits a course, you'll see their results.

### Roadmap

A kanban-style planning view showing all audited courses sorted into three priority lanes:

- **High Priority** — needs attention soon
- **Medium Priority** — important but not urgent
- **Low Priority** — monitor for now

Use the ↑ and ↓ buttons on each card to move courses between priority lanes. Click **View report** to see the full audit details.

### Best Practices

- Audit your highest-traffic or most-linked courses first
- Use the "Missing AI" filter to find courses that haven't addressed AI-related developments
- Use the Roadmap view in team planning discussions to align on what to tackle next
- Re-audit a course after updating it to confirm the issues are resolved

---

## Tips and Limits

- **Drive search covers your personal Drive and files shared with you.** It does not yet cover all of Poynter's shared drives unless those files have been shared with your account.
- **Drive tokens expire after about an hour.** If Drive search stops working mid-session, sign out and back in to refresh.
- **Documents are per-user.** Your saved documents are only visible to you. Audit results are shared across all users.
- **The assistant reads content deeply for evaluation tasks** — expect it to take 30–60 seconds on complex analysis questions while it fetches and reads multiple sources.
