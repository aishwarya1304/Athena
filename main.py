"""
╔══════════════════════════════════════════════════════════════════╗
║          ATHENA RESEARCH AGENT — Python Backend                  ║
║          FastAPI + SQLite + JWT Auth + Gemini Proxy              ║
╚══════════════════════════════════════════════════════════════════╝

QUICK START:
  1. pip install -r requirements.txt
  2. cp .env.example .env   →  fill in GEMINI_API_KEY and SECRET_KEY
  3. python main.py
  4. Open http://localhost:8000

DEPLOY (Railway / Render / any PaaS):
  - Set GEMINI_API_KEY and SECRET_KEY as environment variables
  - Start command: uvicorn main:app --host 0.0.0.0 --port $PORT

API DOCS (auto-generated):
  http://localhost:8000/docs
"""

# ─────────────────────────────────────────────────────────────────────
#  IMPORTS
# ─────────────────────────────────────────────────────────────────────
import os, re, uuid, asyncio, logging
from pathlib import Path
from datetime import datetime, timedelta, timezone
from contextlib import asynccontextmanager
from typing import Optional

import httpx
from dotenv import load_dotenv
from fastapi import FastAPI, Depends, HTTPException, Request, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.security import OAuth2PasswordBearer
from jose import JWTError, jwt
from passlib.context import CryptContext
from pydantic import BaseModel, EmailStr, field_validator
from sqlalchemy import (
    Column, String, Integer, Text, DateTime, Boolean,
    ForeignKey, func, select
)
from sqlalchemy.ext.asyncio import (
    create_async_engine, AsyncSession, async_sessionmaker
)
from sqlalchemy.orm import DeclarativeBase, relationship

load_dotenv()

# ─────────────────────────────────────────────────────────────────────
#  CONFIGURATION  (all from environment / .env file)
# ─────────────────────────────────────────────────────────────────────
GEMINI_API_KEY   = os.getenv("GEMINI_API_KEY", "")
GEMINI_MODEL     = os.getenv("GEMINI_MODEL", "gemini-2.0-flash")
SECRET_KEY       = os.getenv("SECRET_KEY", "CHANGE-THIS-TO-SOMETHING-LONG-AND-RANDOM")
ALGORITHM        = "HS256"
TOKEN_EXPIRE_MIN = int(os.getenv("TOKEN_EXPIRE_MIN", str(60 * 24 * 7)))   # 7 days
DATABASE_URL     = os.getenv("DATABASE_URL", "sqlite+aiosqlite:///./athena.db")
PORT             = int(os.getenv("PORT", "8000"))

# Where the frontend HTML lives (sibling folder called "frontend")
FRONTEND_DIR = Path(__file__).parent / "frontend"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(name)s  %(message)s",
)
log = logging.getLogger("athena")


# ─────────────────────────────────────────────────────────────────────
#  DATABASE  (async SQLite via SQLAlchemy 2.0)
# ─────────────────────────────────────────────────────────────────────
engine  = create_async_engine(DATABASE_URL, echo=False,
            connect_args={"check_same_thread": False} if "sqlite" in DATABASE_URL else {})
Session = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__  = "users"
    id             = Column(String,  primary_key=True, default=lambda: str(uuid.uuid4()))
    name           = Column(String(120), nullable=False)
    email          = Column(String(255), unique=True, nullable=False, index=True)
    password_hash  = Column(String(255), nullable=False)
    is_active      = Column(Boolean, default=True)
    created_at     = Column(DateTime(timezone=True),
                            default=lambda: datetime.now(timezone.utc))
    reports        = relationship("Report", back_populates="user",
                                  cascade="all, delete-orphan", lazy="dynamic")


class Report(Base):
    __tablename__  = "reports"
    id             = Column(String,  primary_key=True, default=lambda: str(uuid.uuid4()))
    user_id        = Column(String,  ForeignKey("users.id", ondelete="CASCADE"),
                            nullable=False, index=True)
    topic          = Column(String(500), nullable=False)
    depth          = Column(String(20),  default="standard")
    sources        = Column(Integer,     default=10)
    word_count     = Column(Integer,     default=0)
    abstract       = Column(Text,        default="")
    html_content   = Column(Text,        nullable=False)
    created_at     = Column(DateTime(timezone=True),
                            default=lambda: datetime.now(timezone.utc))
    user           = relationship("User", back_populates="reports")


async def get_db():
    async with Session() as db:
        try:
            yield db
            await db.commit()
        except Exception:
            await db.rollback()
            raise


# ─────────────────────────────────────────────────────────────────────
#  AUTH HELPERS
# ─────────────────────────────────────────────────────────────────────
pwd_ctx = CryptContext(schemes=["bcrypt"], deprecated="auto")
oauth2  = OAuth2PasswordBearer(tokenUrl="/api/auth/login")


def hash_pw(pw: str) -> str:
    return pwd_ctx.hash(pw)


def verify_pw(pw: str, hsh: str) -> bool:
    return pwd_ctx.verify(pw, hsh)


def make_token(user_id: str) -> str:
    exp = datetime.now(timezone.utc) + timedelta(minutes=TOKEN_EXPIRE_MIN)
    return jwt.encode({"sub": user_id, "exp": exp}, SECRET_KEY, algorithm=ALGORITHM)


async def get_current_user(
    token: str = Depends(oauth2),
    db: AsyncSession = Depends(get_db),
) -> User:
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        uid: str = payload.get("sub")
    except JWTError:
        raise HTTPException(401, "Invalid or expired token",
                            headers={"WWW-Authenticate": "Bearer"})
    row = (await db.execute(
        select(User).where(User.id == uid)
    )).scalar_one_or_none()
    if not row or not row.is_active:
        raise HTTPException(401, "User not found")
    return row


# ─────────────────────────────────────────────────────────────────────
#  GEMINI AI SERVICE
# ─────────────────────────────────────────────────────────────────────
GEMINI_BASE = "https://generativelanguage.googleapis.com/v1beta/models"


def _build_system_prompt(depth: str, date_str: str) -> str:
    word_guide = {
        "quick":    "Write a concise, focused report of 700–900 words. Cover the 3–4 most important findings only.",
        "standard": "Write a thorough report of 1300–1600 words covering all major aspects.",
        "deep":     "Write a comprehensive, deeply analytical report of 2000–2500 words with multiple perspectives and extensive evidence.",
    }.get(depth, "Write a thorough report of 1300–1600 words.")

    return f"""You are Athena, a world-class academic research analyst with live access to Google Search.

ACCURACY RULES — ABSOLUTE, NON-NEGOTIABLE:
1. Use Google Search BEFORE writing to find real, current information.
2. Every fact must come from sources you actually retrieved, or from knowledge you are certain is accurate.
3. NEVER invent statistics, fabricate citations, or make up author names or paper titles.
4. If uncertain about a number, describe findings qualitatively instead.
5. Represent genuine scientific consensus accurately. Note real controversies.
6. This report will be read as authoritative — accuracy is non-negotiable.

OUTPUT RULES:
- Return ONLY valid HTML. No markdown. No code fences. No text outside the HTML.
- Use these exact pre-styled CSS classes:
  • <div class="rep-meta"> — metadata line
  • <p class="abstract-text"> — abstract (gets drop-cap styling)
  • <div class="stat-callout"><span class="stat-number">VALUE</span><span class="stat-label">label [Source Year]</span></div>
  • <div class="sdivider"><span>❦</span></div>
  • <div class="references-section"><h2>References</h2>…</div>
  • <div class="ref-item"><span class="ref-num">N.</span><span>Author. (Year). <em>Title</em>. <a class="ref-link" href="URL" target="_blank">domain.com</a></span></div>
  • <cite>[AuthorYear]</cite> — inline citation

REQUIRED STRUCTURE (all 8 sections, in this exact order):
<h1>[Specific, descriptive title — not just the raw topic]</h1>
<div class="rep-meta">Research Report · {date_str} · Google Search Grounded · {depth} analysis</div>
<h2>Abstract</h2>
<p class="abstract-text">[150–200 word summary. Lead with the single most important finding.]</p>
<h2>1. Introduction</h2>[Why this matters, scope, 2–3 research questions. ~180 words.]
<h2>2. Background &amp; Context</h2>[History, definitions, concepts. Include ONE stat-callout with a REAL verified statistic. ~220 words.]
<h2>3. Key Findings</h2>[Evidence-backed discoveries. Name real researchers/institutions. ~280 words.]
<h2>4. Comparative Insights</h2>[Compare studies, regions, methodologies, expert views. ~220 words.]
<h2>5. Critical Analysis</h2>[Methodology quality, bias, contradictions, evidence gaps. ~180 words.]
<h2>6. Implications &amp; Applications</h2>[Practical, policy, theoretical implications. ~160 words.]
<h2>7. Limitations &amp; Gaps</h2>[What is not covered, open questions, thin evidence. ~120 words.]
<h2>8. Conclusion</h2>[Synthesise 3–4 key takeaways. No new information. ~120 words.]
<div class="sdivider"><span>❦</span></div>
<div class="references-section"><h2>References</h2>[8–15 ref-items with real URLs from search]</div>

{word_guide}
Include 10–15 <cite>[AuthorYear]</cite> citations spread across all sections."""


def _html_escape(s: str) -> str:
    return (s.replace("&", "&amp;").replace("<", "&lt;")
             .replace(">", "&gt;").replace('"', "&quot;"))


def _parse_response(data: dict) -> dict:
    """Extract and clean HTML from Gemini API response."""
    candidates = data.get("candidates", [])
    if not candidates:
        raise ValueError("Gemini returned no candidates in the response.")

    candidate = candidates[0]
    if candidate.get("finishReason") == "SAFETY":
        raise ValueError("Response blocked by safety filters. Try rephrasing your topic.")

    parts    = candidate.get("content", {}).get("parts", [])
    raw_text = "\n".join(p.get("text", "") for p in parts if p.get("text")).strip()

    if not raw_text:
        raise ValueError("Gemini returned an empty response. Please try again.")

    # Strip accidental markdown fences
    html = re.sub(r"^```html\s*", "", raw_text, flags=re.I | re.M)
    html = re.sub(r"^```\s*",     "", html,     flags=re.M)
    html = re.sub(r"```\s*$",     "", html,     flags=re.M).strip()

    # Ensure it starts with an HTML tag
    if not html.lstrip().startswith("<"):
        idx = html.find("<h1")
        if idx >= 0:
            html = html[idx:]

    # Extract grounding metadata
    meta    = candidate.get("groundingMetadata", {})
    queries = meta.get("webSearchQueries", [])
    chunks  = meta.get("groundingChunks", [])

    sources = []
    for c in chunks:
        web = c.get("web", {})
        if web.get("uri"):
            domain = re.sub(r"^https?://(www\.)?", "", web["uri"]).split("/")[0]
            sources.append({"uri": web["uri"], "title": web.get("title", ""), "domain": domain})

    # Append grounding sources if Gemini didn't write a references section
    if sources and "references-section" not in html:
        items = "".join(
            f'<div class="ref-item"><span class="ref-num">{i+1}.</span>'
            f'<span><em>{_html_escape(s["title"] or s["domain"])}</em>. '
            f'<a class="ref-link" href="{s["uri"]}" target="_blank">{s["domain"]}</a>'
            f'</span></div>'
            for i, s in enumerate(sources[:15])
        )
        html += (
            '\n<div class="sdivider"><span>❦</span></div>'
            '\n<div class="references-section"><h2>Sources via Google Search</h2>'
            f'\n{items}\n</div>'
        )

    # Append search query transparency footer
    if queries:
        ql = " · ".join(f"<em>{_html_escape(q)}</em>" for q in queries)
        html += (
            '\n<p style="font-family:var(--font-mono);font-size:0.65rem;'
            'color:var(--text-faint);margin-top:1.5rem;padding-top:0.8rem;'
            'border-top:1px solid var(--border);">'
            f'🔍 Searches used: {ql}</p>'
        )

    word_count = len(re.sub(r"<[^>]+>", "", html).split())

    return {
        "html":           html,
        "word_count":     word_count,
        "search_queries": queries,
        "sources":        sources,
    }


async def call_gemini(topic: str, depth: str, n_sources: int) -> dict:
    """Call Gemini 2.0 Flash with Google Search grounding. Returns parsed result dict."""
    if not GEMINI_API_KEY:
        raise ValueError(
            "GEMINI_API_KEY is not set on the server. "
            "Add it to your .env file or environment variables."
        )

    date_str = datetime.now().strftime("%B %d, %Y")
    payload  = {
        "system_instruction": {
            "parts": [{"text": _build_system_prompt(depth, date_str)}]
        },
        "contents": [{
            "role": "user",
            "parts": [{"text": (
                f'Research and write a complete, accurate HTML report on: "{topic}"\n\n'
                f'Today is {date_str}. Use Google Search first to find current information, '
                'then write the report. Start your response directly with <h1> — '
                'no preamble, no code fences, no markdown.'
            )}],
        }],
        "tools": [{"google_search": {}}],   # ← live Google Search grounding
        "generationConfig": {
            "maxOutputTokens": 8192,
            "temperature":     0.2,
            "topP":            0.8,
            "topK":            40,
        },
    }

    url = f"{GEMINI_BASE}/{GEMINI_MODEL}:generateContent?key={GEMINI_API_KEY}"
    log.info(f"Gemini request | topic='{topic[:60]}' depth={depth}")

    async with httpx.AsyncClient(timeout=120.0) as client:
        for attempt in range(1, 4):
            try:
                resp = await client.post(
                    url, json=payload,
                    headers={"Content-Type": "application/json"},
                )

                if resp.status_code == 429:
                    wait = 15 * attempt
                    log.warning(f"Rate limited — waiting {wait}s (attempt {attempt}/3)")
                    await asyncio.sleep(wait)
                    continue

                if resp.status_code == 400:
                    msg = resp.json().get("error", {}).get("message", "Bad request")
                    raise ValueError(f"Invalid API request: {msg}")

                if resp.status_code == 403:
                    raise PermissionError(
                        "Gemini API key is not authorised. "
                        "Check GEMINI_API_KEY and ensure the Generative Language API is enabled."
                    )

                resp.raise_for_status()
                result = _parse_response(resp.json())
                log.info(
                    f"Gemini OK | words={result['word_count']} "
                    f"sources={len(result['sources'])} queries={len(result['search_queries'])}"
                )
                return result

            except (httpx.TimeoutException, httpx.NetworkError) as e:
                if attempt == 3:
                    raise TimeoutError(
                        "Gemini timed out after 120 seconds. "
                        "Try a simpler topic or use 'quick' depth mode."
                    ) from e
                log.warning(f"Network error, retrying ({attempt}/3): {e}")
                await asyncio.sleep(5 * attempt)

    raise RuntimeError("Research failed after 3 attempts.")


# ─────────────────────────────────────────────────────────────────────
#  PYDANTIC SCHEMAS
# ─────────────────────────────────────────────────────────────────────
class RegisterIn(BaseModel):
    name:     str
    email:    EmailStr
    password: str

    @field_validator("name")
    @classmethod
    def name_ok(cls, v):
        if len(v.strip()) < 2:
            raise ValueError("Name must be at least 2 characters")
        return v.strip()

    @field_validator("password")
    @classmethod
    def pw_ok(cls, v):
        if len(v) < 6:
            raise ValueError("Password must be at least 6 characters")
        return v


class LoginIn(BaseModel):
    email:    EmailStr
    password: str


class ResearchIn(BaseModel):
    topic:     str
    depth:     str = "standard"
    n_sources: int = 10

    @field_validator("topic")
    @classmethod
    def topic_ok(cls, v):
        v = v.strip()
        if len(v) < 3:   raise ValueError("Topic must be at least 3 characters")
        if len(v) > 500: raise ValueError("Topic must be under 500 characters")
        return v

    @field_validator("depth")
    @classmethod
    def depth_ok(cls, v):
        if v not in ("quick", "standard", "deep"):
            raise ValueError("depth must be: quick | standard | deep")
        return v

    @field_validator("n_sources")
    @classmethod
    def src_ok(cls, v):
        return max(5, min(v, 20))


class SaveReportIn(BaseModel):
    topic:        str
    depth:        str = "standard"
    sources:      int = 10
    word_count:   int = 0
    abstract:     str = ""
    html_content: str


# ─────────────────────────────────────────────────────────────────────
#  APP  &  LIFESPAN
# ─────────────────────────────────────────────────────────────────────
@asynccontextmanager
async def lifespan(app: FastAPI):
    log.info("🦉 Athena starting — creating database tables…")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    log.info("✅ Database ready")
    if not GEMINI_API_KEY:
        log.warning("⚠  GEMINI_API_KEY not set — /api/research/generate will fail")
    yield
    log.info("🦉 Athena shutting down")


app = FastAPI(
    title       = "Athena Research Agent API",
    description = "Python backend for Athena — AI research powered by Gemini 2.0 + Google Search",
    version     = "1.0.0",
    lifespan    = lifespan,
    docs_url    = "/api/docs",
    redoc_url   = "/api/redoc",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins     = ["*"],   # tighten to your domain in production
    allow_credentials = True,
    allow_methods     = ["*"],
    allow_headers     = ["*"],
)

# Serve the frontend folder as static files if it exists
if FRONTEND_DIR.is_dir():
    app.mount("/static", StaticFiles(directory=str(FRONTEND_DIR)), name="static")


# ─────────────────────────────────────────────────────────────────────
#  FRONTEND  (serves index.html at root)
# ─────────────────────────────────────────────────────────────────────
@app.get("/", include_in_schema=False)
async def serve_index():
    index = FRONTEND_DIR / "index.html"
    if index.exists():
        return FileResponse(str(index))
    return JSONResponse(
        {"status": "Athena API running",
         "docs":   "/api/docs",
         "note":   "Place your index.html in the /frontend folder to serve the UI"},
        status_code=200,
    )


@app.get("/health", include_in_schema=False)
async def health():
    return {
        "status":     "ok",
        "gemini_key": "configured" if GEMINI_API_KEY else "MISSING — set GEMINI_API_KEY",
        "model":      GEMINI_MODEL,
        "version":    "1.0.0",
    }


# ─────────────────────────────────────────────────────────────────────
#  AUTH ROUTES
# ─────────────────────────────────────────────────────────────────────
@app.post("/api/auth/register", status_code=201, tags=["Auth"])
async def register(body: RegisterIn, db: AsyncSession = Depends(get_db)):
    """Create a new account. Returns JWT token."""
    existing = (await db.execute(
        select(User).where(User.email == body.email.lower())
    )).scalar_one_or_none()

    if existing:
        raise HTTPException(409, "An account with this email already exists.")

    user = User(
        name          = body.name,
        email         = body.email.lower(),
        password_hash = hash_pw(body.password),
    )
    db.add(user)
    await db.flush()
    await db.refresh(user)
    log.info(f"Registered: {user.email}")

    return {
        "access_token": make_token(user.id),
        "token_type":   "bearer",
        "user": {
            "id":         user.id,
            "name":       user.name,
            "email":      user.email,
            "created_at": user.created_at.isoformat(),
        },
    }


@app.post("/api/auth/login", tags=["Auth"])
async def login(body: LoginIn, db: AsyncSession = Depends(get_db)):
    """Login with email + password. Returns JWT token."""
    user = (await db.execute(
        select(User).where(User.email == body.email.lower())
    )).scalar_one_or_none()

    if not user or not verify_pw(body.password, user.password_hash):
        raise HTTPException(401, "Incorrect email or password.")
    if not user.is_active:
        raise HTTPException(403, "Account is deactivated.")
    log.info(f"Login: {user.email}")

    return {
        "access_token": make_token(user.id),
        "token_type":   "bearer",
        "user": {
            "id":         user.id,
            "name":       user.name,
            "email":      user.email,
            "created_at": user.created_at.isoformat(),
        },
    }


@app.get("/api/auth/me", tags=["Auth"])
async def me(u: User = Depends(get_current_user)):
    """Return the current authenticated user's info."""
    return {"id": u.id, "name": u.name,
            "email": u.email, "created_at": u.created_at.isoformat()}


# ─────────────────────────────────────────────────────────────────────
#  RESEARCH ROUTE
# ─────────────────────────────────────────────────────────────────────
@app.post("/api/research/generate", tags=["Research"])
async def generate_report(
    body: ResearchIn,
    u:    User         = Depends(get_current_user),
    db:   AsyncSession = Depends(get_db),
):
    """
    🔬 Main research endpoint.
    Calls Gemini 2.0 Flash with Google Search grounding.
    Returns a fully-formatted HTML research report.
    Requires authentication.
    """
    log.info(f"Research | user={u.email} topic='{body.topic[:60]}' depth={body.depth}")

    try:
        result = await call_gemini(body.topic, body.depth, body.n_sources)
    except ValueError      as e: raise HTTPException(400, str(e))
    except PermissionError as e: raise HTTPException(403, str(e))
    except TimeoutError    as e: raise HTTPException(504, str(e))
    except Exception       as e:
        log.error(f"Research error: {e}", exc_info=True)
        raise HTTPException(500, "Research generation failed. Please try again.")

    return {
        "html_content":      result["html"],
        "word_count":        result["word_count"],
        "search_queries":    result["search_queries"],
        "grounding_sources": result["sources"],
        "model":             GEMINI_MODEL,
        "topic":             body.topic,
        "depth":             body.depth,
    }


# ─────────────────────────────────────────────────────────────────────
#  REPORTS ROUTES  (archive)
# ─────────────────────────────────────────────────────────────────────
@app.get("/api/reports", tags=["Reports"])
async def list_reports(
    page:     int           = 1,
    per_page: int           = 20,
    search:   Optional[str] = None,
    u:        User          = Depends(get_current_user),
    db:       AsyncSession  = Depends(get_db),
):
    """List all saved reports for the current user (paginated, searchable)."""
    q = select(Report).where(Report.user_id == u.id)
    if search:
        p = f"%{search.lower()}%"
        q = q.where(Report.topic.ilike(p) | Report.abstract.ilike(p))

    total = (await db.execute(
        select(func.count()).select_from(q.subquery())
    )).scalar_one()

    rows = (await db.execute(
        q.order_by(Report.created_at.desc())
         .offset((page - 1) * per_page).limit(per_page)
    )).scalars().all()

    return {
        "reports": [
            {
                "id":         r.id,
                "topic":      r.topic,
                "depth":      r.depth,
                "sources":    r.sources,
                "word_count": r.word_count,
                "abstract":   r.abstract,
                "created_at": r.created_at.isoformat(),
            }
            for r in rows
        ],
        "total":    total,
        "page":     page,
        "per_page": per_page,
    }


@app.post("/api/reports", status_code=201, tags=["Reports"])
async def save_report(
    body: SaveReportIn,
    u:    User         = Depends(get_current_user),
    db:   AsyncSession = Depends(get_db),
):
    """Save a generated report to the database archive."""
    count = (await db.execute(
        select(func.count()).where(Report.user_id == u.id)
    )).scalar_one()

    if count >= 200:
        raise HTTPException(400,
            "Archive limit reached (200 reports). Delete old reports to save new ones.")

    r = Report(
        user_id      = u.id,
        topic        = body.topic,
        depth        = body.depth,
        sources      = body.sources,
        word_count   = body.word_count,
        abstract     = body.abstract[:500],
        html_content = body.html_content,
    )
    db.add(r)
    await db.flush()
    await db.refresh(r)
    log.info(f"Saved report: {r.id} user={u.email}")

    return {
        "id":         r.id,
        "topic":      r.topic,
        "depth":      r.depth,
        "sources":    r.sources,
        "word_count": r.word_count,
        "abstract":   r.abstract,
        "created_at": r.created_at.isoformat(),
    }


@app.get("/api/reports/stats", tags=["Reports"])
async def report_stats(
    u:  User         = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Aggregate stats for the user profile page."""
    row = (await db.execute(
        select(
            func.count(Report.id).label("total"),
            func.coalesce(func.sum(Report.word_count), 0).label("words"),
            func.coalesce(func.sum(Report.sources),    0).label("sources"),
        ).where(Report.user_id == u.id)
    )).one()
    return {
        "total_reports":  row.total,
        "total_words":    row.words,
        "total_sources":  row.sources,
    }


@app.get("/api/reports/{report_id}", tags=["Reports"])
async def get_report(
    report_id: str,
    u:         User         = Depends(get_current_user),
    db:        AsyncSession = Depends(get_db),
):
    """Get a single report including its full HTML content."""
    r = (await db.execute(
        select(Report).where(Report.id == report_id, Report.user_id == u.id)
    )).scalar_one_or_none()
    if not r:
        raise HTTPException(404, "Report not found.")
    return {
        "id":           r.id,
        "topic":        r.topic,
        "depth":        r.depth,
        "sources":      r.sources,
        "word_count":   r.word_count,
        "abstract":     r.abstract,
        "html_content": r.html_content,
        "created_at":   r.created_at.isoformat(),
    }


@app.delete("/api/reports/{report_id}", status_code=204, tags=["Reports"])
async def delete_report(
    report_id: str,
    u:         User         = Depends(get_current_user),
    db:        AsyncSession = Depends(get_db),
):
    """Delete a report from the archive."""
    r = (await db.execute(
        select(Report).where(Report.id == report_id, Report.user_id == u.id)
    )).scalar_one_or_none()
    if not r:
        raise HTTPException(404, "Report not found.")
    await db.delete(r)
    log.info(f"Deleted report: {report_id}")


# ─────────────────────────────────────────────────────────────────────
#  GLOBAL ERROR HANDLER
# ─────────────────────────────────────────────────────────────────────
@app.exception_handler(Exception)
async def catch_all(req: Request, exc: Exception):
    log.error(f"Unhandled exception: {exc}", exc_info=True)
    return JSONResponse(
        status_code=500,
        content={"detail": "An unexpected server error occurred. Please try again."},
    )


# ─────────────────────────────────────────────────────────────────────
#  ENTRY POINT
# ─────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    import uvicorn
    log.info(f"🦉 Starting Athena on http://localhost:{PORT}")
    log.info(f"   Gemini key: {'✅ configured' if GEMINI_API_KEY else '❌ MISSING'}")
    log.info(f"   Docs:       http://localhost:{PORT}/api/docs")
    uvicorn.run("main:app", host="0.0.0.0", port=PORT, reload=True)
