import os, re, sqlite3, hashlib, logging, mimetypes
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup
from flask import Flask, render_template, request, redirect, url_for, flash, send_from_directory, abort
from apscheduler.schedulers.background import BackgroundScheduler

try:
    from openai import OpenAI
except ImportError:
    OpenAI = None

BASE = Path(__file__).resolve().parent
DATA = BASE / "data"
FILES = DATA / "files"
DB = DATA / "assessments.db"
FILES.mkdir(parents=True, exist_ok=True)

SOURCE_URLS = [
    os.getenv("SOURCE_URL", "https://studentbooks.moe.gov.eg/Performance_Assessments/"),
    "https://studentbooks.moe.gov.eg/",
]
CHECK_MINUTES = int(os.getenv("CHECK_MINUTES", "20"))
MAX_DOWNLOAD_MB = int(os.getenv("MAX_DOWNLOAD_MB", "25"))
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-5")
UA = os.getenv("BOT_USER_AGENT", "TamerFirstSecondaryAssessments/2.0")

app = Flask(__name__)
app.secret_key = os.getenv("FLASK_SECRET_KEY", "change-this-secret")
logging.basicConfig(level=logging.INFO)
log = logging.getLogger("tamer-pro")

def now():
    return datetime.now(timezone.utc).isoformat()

def db():
    c = sqlite3.connect(DB)
    c.row_factory = sqlite3.Row
    return c

def init_db():
    c = db()
    c.execute("""CREATE TABLE IF NOT EXISTS assessments(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        title TEXT NOT NULL,
        url TEXT UNIQUE NOT NULL,
        source TEXT NOT NULL,
        subject TEXT,
        week TEXT,
        discovered_at TEXT NOT NULL,
        downloaded_path TEXT,
        file_type TEXT,
        status TEXT NOT NULL DEFAULT 'new',
        solution TEXT,
        solved_at TEXT,
        error TEXT,
        fingerprint TEXT UNIQUE
    )""")
    c.commit(); c.close()

def first_secondary(text):
    t = (text or "").lower()
    patterns = [
        "الصف الأول الثانوي", "الصف الاول الثانوي", "أول ثانوي", "اولى ثانوي",
        "first secondary", "1st secondary", "secondary first", "secondary 1"
    ]
    return any(p.lower() in t for p in patterns)

def guess_subject(text):
    t = text or ""
    subjects = {
        "اللغة العربية": ["عربي", "لغة عربية", "arabic"],
        "اللغة الإنجليزية": ["إنجليزي", "انجليزي", "لغة إنجليزية", "english"],
        "الرياضيات": ["رياضيات", "math"],
        "العلوم المتكاملة": ["علوم متكاملة", "علوم", "integrated science"],
        "التاريخ": ["تاريخ", "history"],
        "الفلسفة والمنطق": ["فلسفة", "منطق", "philosophy"],
        "البرمجة والذكاء الاصطناعي": ["برمجة", "ذكاء اصطناعي", "programming", "ai"],
        "التربية الدينية": ["دين", "تربية دينية", "religious"],
    }
    for name, keys in subjects.items():
        if any(k.lower() in t.lower() for k in keys):
            return name
    return "غير محدد"

def guess_week(text):
    m = re.search(r"(?:الأسبوع|الاسبوع|week)\s*[-_: ]?\s*(\d+)", text or "", re.I)
    return f"الأسبوع {m.group(1)}" if m else ""

def discover():
    s = requests.Session()
    s.headers.update({"User-Agent": UA, "Accept": "text/html,application/xhtml+xml"})
    found = {}
    for source in SOURCE_URLS:
        try:
            r = s.get(source, timeout=30)
            r.raise_for_status()
            soup = BeautifulSoup(r.text, "html.parser")
            for a in soup.select("a[href]"):
                href = urljoin(r.url, a.get("href"))
                title = " ".join(a.stripped_strings) or href.rsplit("/",1)[-1]
                parent = a.parent.get_text(" ", strip=True) if a.parent else ""
                context = f"{title} {parent}"
                if not first_secondary(context):
                    # Some pages separate grade from file link; retain likely assessment files.
                    if not any(x in href.lower() for x in [".pdf", ".docx", ".doc"]):
                        continue
                if any(x in href.lower() for x in [".pdf", ".docx", ".doc", "performance", "assessment", "evaluation"]):
                    found[href] = (title, href, source)
        except Exception as e:
            log.warning("Source unavailable %s: %s", source, e)
    c = db(); added = 0
    for href,(title,url,source) in found.items():
        fp = hashlib.sha256(url.encode()).hexdigest()
        try:
            c.execute("""INSERT INTO assessments
              (title,url,source,subject,week,discovered_at,fingerprint)
              VALUES(?,?,?,?,?,?,?)""",
              (title,url,source,guess_subject(title),guess_week(title),now(),fp))
            added += 1
        except sqlite3.IntegrityError:
            pass
    c.commit(); c.close()
    return added

def download_item(row):
    if row["downloaded_path"]:
        return row["downloaded_path"]
    r = requests.get(row["url"], headers={"User-Agent": UA}, timeout=45, stream=True)
    r.raise_for_status()
    length = int(r.headers.get("content-length") or 0)
    if length and length > MAX_DOWNLOAD_MB * 1024 * 1024:
        raise ValueError("الملف أكبر من الحد المسموح.")
    ext = ".pdf"
    ctype = r.headers.get("content-type","").lower()
    if "word" in ctype or "docx" in ctype: ext = ".docx"
    elif "msword" in ctype: ext = ".doc"
    elif "image" in ctype: ext = ".jpg"
    name = hashlib.sha256(row["url"].encode()).hexdigest() + ext
    path = FILES / name
    total = 0
    with open(path,"wb") as f:
        for chunk in r.iter_content(1024*128):
            total += len(chunk)
            if total > MAX_DOWNLOAD_MB * 1024 * 1024:
                f.close()
                path.unlink(missing_ok=True)
                raise ValueError("الملف أكبر من الحد المسموح.")
            f.write(chunk)
    c = db()
    c.execute("UPDATE assessments SET downloaded_path=?,file_type=?,status='downloaded',error=NULL WHERE id=?",
              (str(path.relative_to(BASE)), ext.lstrip("."), row["id"]))
    c.commit(); c.close()
    return str(path.relative_to(BASE))

def extract_text(path):
    p = BASE / path
    suffix = p.suffix.lower()
    if suffix == ".pdf":
        try:
            from pypdf import PdfReader
            reader = PdfReader(str(p))
            return "\n".join((page.extract_text() or "") for page in reader.pages)
        except Exception as e:
            log.warning("PDF text extraction failed: %s", e)
    elif suffix == ".docx":
        try:
            from docx import Document
            d = Document(str(p))
            return "\n".join(x.text for x in d.paragraphs)
        except Exception as e:
            log.warning("DOCX extraction failed: %s", e)
    return ""

def solve_row(row):
    if not OpenAI or not os.getenv("OPENAI_API_KEY"):
        return "الحل التلقائي غير مفعل: أضف OPENAI_API_KEY إلى متغيرات البيئة على الخادم."
    path = download_item(row)
    text = extract_text(path)
    client = OpenAI()
    instructions = """أنت مدرس مصري للصف الأول الثانوي. حل التقييم المرفق/المستخرج بدقة.
قواعد:
- لا تخترع سؤالًا غير موجود.
- رقّم كل سؤال.
- اكتب الإجابة النهائية بوضوح.
- في الاختيار من متعدد اذكر الاختيار الصحيح ثم سبب مختصر.
- في الرياضيات والعلوم وضّح خطوات الحل.
- إذا كان الملف صورة أو PDF ممسوحًا ولم يتوفر نص، لا تخمّن؛ قل إن محتوى السؤال يحتاج رؤية الصورة.
- لا تقل إن الحل رسمي من الوزارة؛ هو حل مساعد مولد آليًا.
"""
    # Prefer file input when possible; this lets the model inspect PDFs directly.
    try:
        with open(BASE / path, "rb") as f:
            uploaded = client.files.create(file=f, purpose="user_data")
        resp = client.responses.create(
            model=OPENAI_MODEL,
            instructions=instructions,
            input=[{"role":"user","content":[
                {"type":"input_file","file_id":uploaded.id},
                {"type":"input_text","text":f"العنوان: {row['title']}\nالمادة: {row['subject']}\nالأسبوع: {row['week']}\nحل التقييم كاملًا."}
            ]}],
            store=False
        )
        return resp.output_text
    except Exception as e:
        log.warning("File solve failed, text fallback: %s", e)
        if not text.strip():
            return f"تعذر قراءة الملف تلقائيًا. الخطأ: {e}"
        resp = client.responses.create(
            model=OPENAI_MODEL, instructions=instructions,
            input=f"{row['title']}\n{row['subject']}\n{text[:60000]}",
            store=False
        )
        return resp.output_text

def process_pending():
    c = db()
    rows = c.execute("SELECT * FROM assessments WHERE status IN ('new','downloaded') AND solution IS NULL ORDER BY id").fetchall()
    done = 0
    for row in rows:
        try:
            c.execute("UPDATE assessments SET status='processing',error=NULL WHERE id=?", (row["id"],)); c.commit()
            sol = solve_row(row)
            c.execute("UPDATE assessments SET status='solved',solution=?,solved_at=?,error=NULL WHERE id=?",
                      (sol,now(),row["id"]))
            c.commit(); done += 1
        except Exception as e:
            c.execute("UPDATE assessments SET status='error',error=? WHERE id=?", (str(e),row["id"]))
            c.commit()
    c.close()
    return done

def automatic_job():
    try:
        new = discover()
        solved = process_pending()
        log.info("Automatic job: %s new, %s solved", new, solved)
    except Exception:
        log.exception("Automatic job failed")

@app.route("/")
def home():
    q = request.args.get("q","").strip()
    subject = request.args.get("subject","").strip()
    c = db()
    sql = "SELECT * FROM assessments WHERE 1=1"
    args=[]
    if q: sql += " AND (title LIKE ? OR subject LIKE ? OR week LIKE ?)"; args += [f"%{q}%"]*3
    if subject: sql += " AND subject=?"; args.append(subject)
    sql += " ORDER BY id DESC"
    rows = c.execute(sql,args).fetchall()
    subjects = [x[0] for x in c.execute("SELECT DISTINCT subject FROM assessments WHERE subject IS NOT NULL AND subject<>'' ORDER BY subject")]
    c.close()
    return render_template("index.html",rows=rows,subjects=subjects,q=q,subject=subject)

@app.post("/refresh")
def refresh():
    try:
        n=discover(); s=process_pending()
        flash(f"تم التحديث: اكتشفت {n} تقييم جديد، وتم حل {s} تقييم.", "ok")
    except Exception as e: flash(str(e),"err")
    return redirect(url_for("home"))

@app.route("/assessment/<int:item_id>")
def view(item_id):
    c=db(); row=c.execute("SELECT * FROM assessments WHERE id=?",(item_id,)).fetchone(); c.close()
    if not row: abort(404)
    return render_template("assessment.html",row=row)

@app.route("/files/<path:name>")
def files(name):
    return send_from_directory(FILES, Path(name).name)

@app.post("/solve/<int:item_id>")
def solve_one(item_id):
    c=db(); row=c.execute("SELECT * FROM assessments WHERE id=?",(item_id,)).fetchone(); c.close()
    if not row: abort(404)
    try:
        sol=solve_row(row)
        c=db(); c.execute("UPDATE assessments SET solution=?,status='solved',solved_at=?,error=NULL WHERE id=?",(sol,now(),item_id)); c.commit(); c.close()
        flash("تم حل التقييم.", "ok")
    except Exception as e:
        c=db(); c.execute("UPDATE assessments SET status='error',error=? WHERE id=?",(str(e),item_id)); c.commit(); c.close()
        flash(f"تعذر الحل: {e}","err")
    return redirect(url_for("view",item_id=item_id))

if __name__=="__main__":
    init_db()
    scheduler=BackgroundScheduler(daemon=True)
    scheduler.add_job(automatic_job,"interval",minutes=CHECK_MINUTES,id="auto",replace_existing=True)
    scheduler.start()
    try: automatic_job()
    except Exception: pass
    app.run(host="0.0.0.0",port=int(os.getenv("PORT","8000")))
