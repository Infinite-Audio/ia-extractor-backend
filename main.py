import os, uuid, tempfile, shutil, asyncio, subprocess
from fastapi import FastAPI, UploadFile, File
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import boto3

# For first test we allow all; after it works we'll lock this to your domains.
ALLOWED_ORIGINS = ["*"]

S3_BUCKET      = os.getenv("S3_BUCKET")
S3_PUBLIC_BASE = os.getenv("S3_PUBLIC_BASE")   # e.g. https://pub-XXXX.r2.dev/infiniteaudio-stems
AWS_ENDPOINT_URL      = os.getenv("AWS_ENDPOINT_URL")
AWS_ACCESS_KEY_ID     = os.getenv("AWS_ACCESS_KEY_ID")
AWS_SECRET_ACCESS_KEY = os.getenv("AWS_SECRET_ACCESS_KEY")

s3 = boto3.client(
    "s3",
    aws_access_key_id=AWS_ACCESS_KEY_ID,
    aws_secret_access_key=AWS_SECRET_ACCESS_KEY,
    endpoint_url=AWS_ENDPOINT_URL,
)

app = FastAPI(title="Infinite Audio — 4‑Stem Extractor")
app.add_middleware(CORSMiddleware, allow_origins=ALLOWED_ORIGINS, allow_methods=["*"], allow_headers=["*"])

JOBS = {}

class JobOut(BaseModel):
    id: str
    status: str
    detail: str | None = None
    downloads: dict | None = None
    error: str | None = None

@app.post("/jobs", response_model=JobOut)
async def create_job(file: UploadFile = File(...)):
    jid = str(uuid.uuid4())
    JOBS[jid] = {"status":"queued","detail":None,"downloads":None,"error":None}
    tmpdir = tempfile.mkdtemp(prefix=f"job_{jid}_")
    src = os.path.join(tmpdir, file.filename)
    with open(src, "wb") as f:
        f.write(await file.read())
    asyncio.create_task(run(jid, src, tmpdir))
    return JobOut(id=jid, status="queued", detail="Waiting for worker")

@app.get("/jobs/{jid}", response_model=JobOut)
def get_job(jid: str):
    j = JOBS.get(jid)
    if not j:
        return JobOut(id=jid, status="error", error="Not found")
    return JobOut(id=jid, status=j["status"], detail=j.get("detail"),
                  downloads=j.get("downloads"), error=j.get("error"))

async def run(jid: str, inpath: str, tmpdir: str):
    try:
        JOBS[jid]["status"]="running"; JOBS[jid]["detail"]="Separating stems…"
        outdir = os.path.join(tmpdir, "out"); os.makedirs(outdir, exist_ok=True)

        # Demucs v4 model (4 stems)
        subprocess.run(["demucs","-n","htdemucs_ft","-o",outdir,inpath], check=True)

        sep_root = os.path.join(outdir, "htdemucs_ft")
        base = os.path.splitext(os.path.basename(inpath))[0]
        stems_dir = os.path.join(sep_root, base)

        downloads = {}
        for stem in ["vocals","drums","bass","other"]:
            p = os.path.join(stems_dir, f"{stem}.wav")
            key = f"stems/{jid}/{stem}.wav"
            s3.upload_file(p, S3_BUCKET, key, ExtraArgs={"ContentType":"audio/wav"})
            downloads[stem] = f"{S3_PUBLIC_BASE.rstrip('/')}/{key}"
        JOBS[jid]["status"]="done"; JOBS[jid]["downloads"]=downloads
    except subprocess.CalledProcessError:
        JOBS[jid]["status"]="error"; JOBS[jid]["error"]="Processing failed (model)"
    except Exception as e:
        JOBS[jid]["status"]="error"; JOBS[jid]["error"]=str(e)[:300]
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)
