# YouTube Downloader — Build Spec

Build a single-instance web app that downloads a YouTube video as MP4 or MP3 using `yt-dlp` and `ffmpeg`, running on one AWS EC2 instance.

Work through this in the order the phases are listed. After each phase, stop and tell me how to verify it works before moving on.

---

## Stack

- **Backend:** Python 3.11, FastAPI, Uvicorn
- **Frontend:** one static `index.html` with vanilla JS and plain CSS. No framework, no build step, no npm.
- **External binaries:** `yt-dlp` and `ffmpeg`, called as subprocesses
- **Host:** one Ubuntu 24.04 EC2 instance (t3.small)
- **State:** an in-memory Python dict. No database.
- **Storage:** local disk at `/tmp/jobs/{job_id}/`. No S3.

---

## Architecture

```
Browser  --POST /api/jobs-------->  FastAPI
                                      |
                                      v
                                 subprocess: yt-dlp (+ ffmpeg)
                                      |
                                      v
                                 /tmp/jobs/{job_id}/output.mp4
Browser  <--GET /api/jobs/{id}----  status JSON (polled every 1s)
Browser  <--GET /api/jobs/{id}/file--  the file itself
```

The browser never talks to YouTube. All work happens on the instance.

---

## API

### `POST /api/jobs`
Request: `{"url": "<youtube url>", "format": "mp4" | "mp3"}`

- Validate the URL is a YouTube URL. Reject anything else with a 400.
- Generate a `job_id` (uuid4).
- Start the download in a FastAPI `BackgroundTasks` job.
- Respond immediately: `{"job_id": "..."}`

### `GET /api/jobs/{job_id}`
Response:
```json
{
  "status": "downloading" | "processing" | "done" | "error",
  "percent": 0-100,
  "title": "video title or null",
  "error": "message or null"
}
```

`percent` only applies while `status` is `downloading`. Once yt-dlp finishes the raw download and ffmpeg starts merging or transcoding, set `status` to `processing` — there is no reliable percentage for that stage, so the UI shows an indeterminate state instead of a stuck bar.

### `GET /api/jobs/{job_id}/file`
Returns the finished file as a `FileResponse` with `Content-Disposition: attachment` and the video title as the filename (sanitised — strip anything that isn't alphanumeric, space, dash or underscore). Return 404 if the job isn't `done`.

---

## The yt-dlp commands

These are the two commands. Do not substitute your own format selectors.

**MP4 (highest quality video):**
```
yt-dlp -f "bv*[ext=mp4]+ba[ext=m4a]/b[ext=mp4]/b" \
       --merge-output-format mp4 \
       --newline \
       -o "/tmp/jobs/{job_id}/output.%(ext)s" \
       "{url}"
```

**MP3 (highest quality audio):**
```
yt-dlp -f "ba/b" \
       -x --audio-format mp3 --audio-quality 0 \
       --newline \
       -o "/tmp/jobs/{job_id}/output.%(ext)s" \
       "{url}"
```

`--newline` is what makes progress parseable: yt-dlp prints each progress update on its own line instead of overwriting one line with carriage returns.

**Parsing progress:** read stdout line by line. Lines matching `[download]   45.2% of ...` give you the percent. When you see `[Merger]` or `[ExtractAudio]`, switch the job status to `processing`. When the process exits with code 0, set `done`; non-zero, set `error` and store the last few lines of stderr.

Get the video title with a separate `yt-dlp --print "%(title)s" --skip-download "{url}"` call before starting the main download, so the UI can show it.

---

## Frontend

One page. No emoji, no gradients, no icon library, no animation beyond the progress bar filling.

Layout, top to bottom:

1. **Format toggle** — two buttons side by side, `Video (MP4)` and `Audio (MP3)`. Exactly one is selected at a time; the selected one is visually distinct (filled vs outlined). Default to Video.
2. **URL input** — a single full-width text field, placeholder `Paste YouTube link`.
3. **Download button** — full width. Disabled until the input is non-empty.
4. **Status area** — hidden until a job starts. Shows the video title, then either a progress bar with a percentage, the word `Processing`, or an error message.
5. **Get file button** — replaces the status area when the job is done. Clicking it hits `/api/jobs/{job_id}/file`.

Behaviour: on Download, POST the job, then poll `GET /api/jobs/{job_id}` every 1000ms until status is `done` or `error`. Stop polling either way.

Styling: system font stack, one accent colour, generous whitespace, max-width around 600px centred. Everything in `<style>` in the same file.

---

## Cleanup

A background task that runs every 10 minutes and deletes any directory under `/tmp/jobs/` older than 30 minutes, plus its dict entry. Without this the disk fills and the instance dies quietly.

---

## Deployment

Give me, as a separate file `DEPLOY.md`:

- The exact commands to install Python, ffmpeg and yt-dlp on a fresh Ubuntu 24.04 EC2 instance
- Which security group ports to open and why
- A systemd unit file so the app restarts on reboot and on crash
- How to see the logs when it breaks

Do not set up nginx, TLS, a domain, or Docker. Plain HTTP on the instance's public IP is the MVP.

---

## Explicitly out of scope

Do not build, suggest, or leave hooks for any of these:

- User accounts, login, sessions
- A database
- S3 or any AWS SDK usage
- Playlists, channels, or batch downloads
- Quality/resolution selection beyond the two fixed presets
- Subtitles, thumbnails, or metadata embedding
- Job queues, Celery, Redis
- WebSockets or server-sent events
- Docker
- Tests

If you think one of these is necessary, say so and explain why before writing any of it.

---

## How to work with me

I am a customer support consultant with a business analytics degree, not a developer. I read code better than I write it.

- Explain **why** before **what** when you make a design decision I didn't specify.
- Comment anything non-obvious, especially the subprocess and stdout-parsing logic.
- Build in the phase order below. Stop at each checkpoint and give me a concrete way to verify it before continuing.

**Phase 1:** FastAPI app with the three endpoints, returning hardcoded fake data. No yt-dlp yet.
*Checkpoint: I can curl all three endpoints and see sensible responses.*

**Phase 2:** Wire in the real yt-dlp subprocess and progress parsing, still with no frontend.
*Checkpoint: I can POST a real URL via curl, poll status, and find a real file on disk.*

**Phase 3:** The frontend page.
*Checkpoint: The whole flow works end to end on my Mac at localhost.*

**Phase 4:** Cleanup task and `DEPLOY.md`.
*Checkpoint: It works on the EC2 instance from my phone's browser.*
