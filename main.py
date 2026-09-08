"""YouTube Downloader — Phase 2.

Real yt-dlp subprocess with progress parsing. Still no frontend.

How a job flows:
  POST /api/jobs   -> validate URL, create job dict, kick off run_download()
                      as a FastAPI background task, return the job_id at once
  run_download()   -> (in the background) fetch the title, then run yt-dlp,
                      reading its output line by line to update the job dict
  GET /api/jobs/id -> just reads the job dict; the background task is the
                      only writer, so this endpoint does no work itself
  GET .../file     -> serves the finished file from /tmp/jobs/{job_id}/
"""

import collections
import pathlib
import re
import subprocess
import uuid
from urllib.parse import quote

from fastapi import BackgroundTasks, FastAPI, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel

app = FastAPI()

JOBS_DIR = pathlib.Path("/tmp/jobs")

# The in-memory job store from the spec. Keys are job_id strings, values are
# dicts shaped like the GET /api/jobs/{id} response, plus private fields
# (leading underscore) that are stripped before the dict is returned.
jobs: dict[str, dict] = {}

# Matches the YouTube URL shapes people actually paste:
#   https://www.youtube.com/watch?v=...   https://youtu.be/...
#   https://m.youtube.com/watch?v=...     https://music.youtube.com/...
# Anything else (vimeo, a random string, a bare video id) is rejected.
YOUTUBE_URL_RE = re.compile(
    r"^https?://(www\.|m\.|music\.)?(youtube\.com|youtu\.be)/\S+$"
)

# yt-dlp progress lines (thanks to --newline, one per line) look like:
#   [download]  45.2% of   10.32MiB at    2.11MiB/s ETA 00:02
# This pulls out the "45.2".
PROGRESS_RE = re.compile(r"^\[download\]\s+(\d+(?:\.\d+)?)%")


class JobRequest(BaseModel):
    url: str
    format: str


def build_command(url: str, fmt: str, out_dir: pathlib.Path) -> list[str]:
    """The two yt-dlp commands from the spec, verbatim, as argument lists.

    Passing a list (not a shell string) means the URL is handed to yt-dlp
    as a single argument — nothing in it can be interpreted by a shell.
    """
    if fmt == "mp4":
        return [
            "yt-dlp", "--no-playlist",
            "-f", "bv*[ext=mp4]+ba[ext=m4a]/b[ext=mp4]/b",
            "--merge-output-format", "mp4",
            "--newline",
            "-o", f"{out_dir}/output.%(ext)s",
            url,
        ]
    return [
        "yt-dlp", "--no-playlist",
        "-f", "ba/b",
        "-x", "--audio-format", "mp3", "--audio-quality", "0",
        "--newline",
        "-o", f"{out_dir}/output.%(ext)s",
        url,
    ]


def run_download(job_id: str, url: str, fmt: str) -> None:
    """The background task: everything that happens after POST returns.

    FastAPI runs this in a worker thread, so it's fine that it blocks for
    the whole download. It is the only writer to this job's dict entry;
    the GET endpoints only read.
    """
    job = jobs[job_id]
    out_dir = JOBS_DIR / job_id
    out_dir.mkdir(parents=True, exist_ok=True)

    # --- Step 1: fetch the title in a separate quick yt-dlp call (per the
    # spec) so the UI can show it while the download runs. If this fails,
    # the URL is bad (private/deleted video, etc.) and the download would
    # fail the same way, so we report the error now instead of trying.
    try:
        title_proc = subprocess.run(
            ["yt-dlp", "--no-playlist", "--print", "%(title)s",
             "--skip-download", url],
            capture_output=True, text=True, timeout=60,
        )
    except subprocess.TimeoutExpired:
        job["status"] = "error"
        job["error"] = "timed out fetching video info"
        return
    if title_proc.returncode != 0:
        job["status"] = "error"
        job["error"] = "\n".join(title_proc.stderr.strip().splitlines()[-3:])
        return
    job["title"] = title_proc.stdout.strip() or None

    # --- Step 2: the real download. stderr is merged into stdout
    # (stderr=STDOUT) so there is only one stream to read; reading two
    # pipes from one thread can deadlock when the unread one fills up.
    proc = subprocess.Popen(
        build_command(url, fmt, out_dir),
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
    )

    # Keep only the last few output lines: if yt-dlp fails, these are
    # what we show as the error message.
    tail: collections.deque = collections.deque(maxlen=5)

    for raw_line in proc.stdout:
        line = raw_line.strip()
        if not line:
            continue
        tail.append(line)

        # "[Merger]" (mp4: joining video+audio) or "[ExtractAudio]" (mp3:
        # transcoding) means ffmpeg has taken over. No reliable percentage
        # exists for that stage, so the status switches to "processing".
        if line.startswith("[Merger]") or line.startswith("[ExtractAudio]"):
            job["status"] = "processing"

        # Only track percent while still downloading. Note the bar can
        # jump backwards once mid-download: for mp4, yt-dlp downloads the
        # video stream 0-100% and then the audio stream 0-100%. Harmless,
        # and per the spec we don't do anything fancier about it.
        elif job["status"] == "downloading":
            match = PROGRESS_RE.match(line)
            if match:
                job["percent"] = min(100, int(float(match.group(1))))

    returncode = proc.wait()

    if returncode != 0:
        job["status"] = "error"
        job["error"] = "\n".join(tail) or f"yt-dlp exited with code {returncode}"
        return

    # yt-dlp exited cleanly, so output.mp4 / output.mp3 should exist.
    # Double-check before declaring victory — a missing file here would
    # otherwise turn into a confusing 404 at download time.
    out_file = out_dir / f"output.{fmt}"
    if not out_file.exists():
        job["status"] = "error"
        job["error"] = "yt-dlp finished but the output file is missing"
        return

    job["_file_path"] = str(out_file)
    job["status"] = "done"
    job["percent"] = 100


@app.post("/api/jobs")
def create_job(req: JobRequest, background_tasks: BackgroundTasks):
    if req.format not in ("mp4", "mp3"):
        raise HTTPException(status_code=400, detail="format must be 'mp4' or 'mp3'")
    if not YOUTUBE_URL_RE.match(req.url):
        raise HTTPException(status_code=400, detail="not a YouTube URL")

    job_id = str(uuid.uuid4())
    jobs[job_id] = {
        "status": "downloading",
        "percent": 0,
        "title": None,
        "error": None,
        "_format": req.format,
        "_file_path": None,
    }
    background_tasks.add_task(run_download, job_id, req.url, req.format)
    return {"job_id": job_id}


@app.get("/api/jobs/{job_id}")
def get_job(job_id: str):
    job = jobs.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="no such job")
    # Return the spec's response shape, without the private fields.
    return {k: v for k, v in job.items() if not k.startswith("_")}


# Characters that are invalid in filenames on Windows (< > : " / \ | ? *)
# or on Mac (: and /), plus ASCII control characters. Everything else —
# including non-ASCII letters like Hebrew — is kept.
INVALID_FILENAME_CHARS_RE = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def content_disposition(title: str, ext: str) -> str:
    """Build a Content-Disposition header that names the download correctly
    everywhere, per RFC 6266.

    The header carries the filename twice, on purpose:
      filename=   ASCII only. The fallback that old or strict parsers read —
                  curl -OJ, notably, refuses to read anything else.
      filename*=  UTF-8, percent-encoded (RFC 5987). Carries the real title,
                  Hebrew and all; modern browsers prefer it when present.

    (Starlette's own FileResponse(filename=...) emits only filename*= as soon
    as the name needs any encoding at all — even for a space — which is why
    curl saved the file as "file". Hence building the header by hand.)
    """
    # Strip characters that can't appear in a filename, then trailing dots
    # and spaces (Windows rejects those at the end of a name).
    clean = INVALID_FILENAME_CHARS_RE.sub("", title or "").strip(" .") or "download"

    # ASCII fallback: drop every non-ASCII character. If no letters or
    # digits survive (e.g. an all-Hebrew title leaves only stray dashes),
    # fall back to the word "download" rather than a punctuation-only name.
    fallback = clean.encode("ascii", "ignore").decode().strip(" .")
    if not re.search(r"[A-Za-z0-9]", fallback):
        fallback = "download"

    encoded = quote(clean, safe="")  # percent-encode the UTF-8 form
    return (
        f'attachment; filename="{fallback}.{ext}"; '
        f"filename*=UTF-8''{encoded}.{ext}"
    )


@app.get("/api/jobs/{job_id}/file")
def get_file(job_id: str):
    job = jobs.get(job_id)
    if job is None or job["status"] != "done" or not job["_file_path"]:
        raise HTTPException(status_code=404, detail="job not done")

    fmt = job["_format"]
    return FileResponse(
        job["_file_path"],
        media_type="video/mp4" if fmt == "mp4" else "audio/mpeg",
        headers={"Content-Disposition": content_disposition(job["title"], fmt)},
    )
