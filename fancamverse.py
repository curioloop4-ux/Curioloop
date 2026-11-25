import json
import math
import os
import re
import subprocess
import tempfile
import wave
from pathlib import Path

import cv2
import numpy as np
from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload, MediaIoBaseDownload
import io

SCOPES = ["https://www.googleapis.com/auth/drive"]
ROOT_ID = os.environ.get("FANCAMVERSE_ROOT_FOLDER_ID", "").strip()
SERVICE_JSON = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON", "").strip()
MAX_SOURCES = int(os.environ.get("MAX_SOURCES_PER_RUN", "1"))
CLIPS_PER_SOURCE = int(os.environ.get("CLIPS_PER_SOURCE", "3"))
CLIP_SECONDS = int(os.environ.get("CLIP_SECONDS", "24"))
WORK = Path("work")
WORK.mkdir(exist_ok=True)


def drive_client():
    if not ROOT_ID or not SERVICE_JSON:
        raise RuntimeError("Missing GOOGLE_SERVICE_ACCOUNT_JSON or FANCAMVERSE_ROOT_FOLDER_ID GitHub secret.")
    info = json.loads(SERVICE_JSON)
    creds = Credentials.from_service_account_info(info, scopes=SCOPES)
    return build("drive", "v3", credentials=creds, cache_discovery=False)


def find_or_create_folder(service, name, parent_id):
    q = (
        "mimeType='application/vnd.google-apps.folder' "
        f"and name='{name}' and '{parent_id}' in parents and trashed=false"
    )
    result = service.files().list(q=q, fields="files(id,name)", pageSize=20).execute()
    if result.get("files"):
        return result["files"][0]["id"]
    folder = service.files().create(
        body={"name": name, "mimeType": "application/vnd.google-apps.folder", "parents": [parent_id]},
        fields="id"
    ).execute()
    return folder["id"]


def download_file(service, file_id, destination):
    request = service.files().get_media(fileId=file_id)
    with io.FileIO(destination, "wb") as fh:
        downloader = MediaIoBaseDownload(fh, request, chunksize=8 * 1024 * 1024)
        done = False
        while not done:
            _, done = downloader.next_chunk()


def upload_file(service, path, parent_id, mime_type):
    metadata = {"name": Path(path).name, "parents": [parent_id]}
    media = MediaFileUpload(str(path), mimetype=mime_type, resumable=True)
    return service.files().create(body=metadata, media_body=media, fields="id,name,webViewLink").execute()


def ffprobe_duration(path):
    cmd = [
        "ffprobe", "-v", "error", "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1", str(path)
    ]
    return float(subprocess.check_output(cmd, text=True).strip())


def audio_energy(path, duration):
    wav_path = WORK / "analysis_audio.wav"
    subprocess.run(
        ["ffmpeg", "-y", "-i", str(path), "-vn", "-ac", "1", "-ar", "8000",
         "-f", "wav", str(wav_path)],
        check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
    )
    with wave.open(str(wav_path), "rb") as wf:
        rate = wf.getframerate()
        raw = wf.readframes(wf.getnframes())
    samples = np.frombuffer(raw, dtype=np.int16).astype(np.float32)
    if samples.size == 0:
        return np.zeros(max(1, int(duration)))
    frame = rate
    values = []
    for start in range(0, len(samples), frame):
        chunk = samples[start:start + frame]
        values.append(float(np.sqrt(np.mean(chunk * chunk))) if len(chunk) else 0.0)
    values = np.asarray(values, dtype=np.float32)
    scale = float(np.percentile(values, 90)) if values.size else 1.0
    return values / max(scale, 1.0)


def visual_motion(path, duration):
    cap = cv2.VideoCapture(str(path))
    fps = cap.get(cv2.CAP_PROP_FPS) or 25.0
    total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
    seconds = max(1, int(math.ceil(duration)))
    scores = np.zeros(seconds, dtype=np.float32)
    previous = None
    for sec in range(seconds):
        cap.set(cv2.CAP_PROP_POS_MSEC, sec * 1000)
        ok, frame = cap.read()
        if not ok:
            continue
        frame = cv2.resize(frame, (160, 90))
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        if previous is not None:
            scores[sec] = float(np.mean(cv2.absdiff(gray, previous))) / 32.0
        previous = gray
    cap.release()
    scale = float(np.percentile(scores, 90)) if scores.size else 1.0
    return scores / max(scale, 0.1)


def choose_segments(path, duration, count, clip_seconds):
    if duration < 8:
        return []
    length = min(clip_seconds, max(8, int(duration // 1)))
    audio = audio_energy(path, duration)
    motion = visual_motion(path, duration)
    seconds = min(len(audio), len(motion), int(duration))
    if seconds < 1:
        return []
    activity = 0.55 * audio[:seconds] + 0.45 * motion[:seconds]
    window = min(length, seconds)
    candidates = []
    step = max(2, window // 5)
    for start in range(0, max(1, seconds - window + 1), step):
        end = min(seconds, start + window)
        score = float(np.mean(activity[start:end]))
        candidates.append((score, start, end))
    candidates.sort(reverse=True)
    selected = []
    for score, start, end in candidates:
        if all(end <= old_start or start >= old_end for _, old_start, old_end in selected):
            selected.append((score, start, end))
        if len(selected) >= count:
            break
    if not selected:
        selected = [(0.0, max(0, (seconds - window) // 2), min(seconds, max(0, (seconds - window) // 2) + window))]
    return [(start, end, score) for score, start, end in sorted(selected, key=lambda x: x[1])]


def load_metadata(video_path, drive_files):
    base = Path(video_path).stem
    sidecar = next((f for f in drive_files if Path(f["name"]).stem == base and f["name"].lower().endswith(".json")), None)
    data = {}
    if sidecar:
        tmp = WORK / (base + ".json")
        download_file(service_global, sidecar["id"], tmp)
        try:
            data = json.loads(tmp.read_text(encoding="utf-8"))
        except Exception:
            data = {}
    parts = [p.strip() for p in re.split(r"__|_", base) if p.strip()]
    for key, idx in [("group", 0), ("member", 1), ("song", 2), ("event", 3)]:
        if not data.get(key) and len(parts) > idx:
            data[key] = parts[idx].replace("-", " ").title()
    return data


def make_metadata(meta, index):
    group = meta.get("group", "").strip()
    member = meta.get("member", "").strip()
    song = meta.get("song", "").strip()
    event = meta.get("event", "").strip()
    who = " ".join(x for x in [group, member] if x)
    if who and song:
        title = f"{who} - {song} | Fancam Highlight #{index} #Shorts"
    elif who:
        title = f"{who} | K-pop Fancam Highlight #{index} #Shorts"
    else:
        title = f"K-pop Stage Highlight #{index} | FANCAMVERSE #Shorts"
    title = title[:98]
    description = "A standout moment from a K-pop stage.\n\n"
    if who:
        description += f"Artist: {who}\n"
    if song:
        description += f"Song: {song}\n"
    if event:
        description += f"Event: {event}\n"
    description += "\nFollow FANCAMVERSE for more K-pop stage moments.\n#Kpop #Fancam #Shorts"
    return {
        "title": title,
        "description": description,
        "tags": ["K-pop", "fancam", "KpopShorts", "KpopPerformance", "idol", "Shorts"],
        "source_metadata": meta,
        "clip_number": index
    }


def render_clip(source, start, end, output):
    duration = max(1, end - start)
    vf = "scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920,fps=30,setsar=1"
    cmd = [
        "ffmpeg", "-y", "-ss", str(start), "-i", str(source), "-t", str(duration),
        "-vf", vf, "-map", "0:v:0", "-map", "0:a?", "-c:v", "libx264",
        "-preset", "veryfast", "-crf", "19", "-c:a", "aac", "-b:a", "192k",
        "-movflags", "+faststart", "-shortest", str(output)
    ]
    subprocess.run(cmd, check=True)


def process_source(service, inbox_id, review_id, processed_id, source, all_files):
    source_name = source["name"]
    local_source = WORK / source_name
    download_file(service, source["id"], local_source)
    duration = ffprobe_duration(local_source)
    if duration < 8:
        raise RuntimeError(f"Source video is too short ({duration:.1f}s).")
    meta = load_metadata(local_source, all_files)
    segments = choose_segments(local_source, duration, CLIPS_PER_SOURCE, CLIP_SECONDS)
    if not segments:
        raise RuntimeError("No usable highlight segments found.")
    batch_dir = WORK / re.sub(r"[^A-Za-z0-9_-]+", "_", Path(source_name).stem)
    batch_dir.mkdir(parents=True, exist_ok=True)
    for index, (start, end, score) in enumerate(segments, 1):
        output = batch_dir / f"{Path(source_name).stem}_short_{index:02d}.mp4"
        render_clip(local_source, start, end, output)
        details = make_metadata(meta, index)
        details.update({"source_file": source_name, "start_seconds": start, "end_seconds": end,
                        "activity_score": round(score, 4), "review_required": True})
        upload_file(service, output, review_id, "video/mp4")
        sidecar = output.with_suffix(".json")
        sidecar.write_text(json.dumps(details, ensure_ascii=False, indent=2), encoding="utf-8")
        upload_file(service, sidecar, review_id, "application/json")
        print(f"REVIEW_READY: {output.name} ({start}-{end}s)")
    service.files().update(
        fileId=source["id"],
        body={"appProperties": {"fancamverse_status": "review_ready"}},
        fields="id,appProperties"
    ).execute()
    print(f"PROCESSED_SOURCE: {source_name}")


service_global = None


def main():
    global service_global
    service = drive_client()
    service_global = service
    inbox_id = find_or_create_folder(service, "INBOX", ROOT_ID)
    review_id = find_or_create_folder(service, "REVIEW", ROOT_ID)
    processed_id = find_or_create_folder(service, "PROCESSED", ROOT_ID)
    query = (
        f"'{inbox_id}' in parents and trashed=false and "
        "(mimeType contains 'video/')"
    )
    result = service.files().list(
        q=query, pageSize=100, orderBy="createdTime",
        fields="files(id,name,mimeType,appProperties,createdTime)"
    ).execute()
    files = result.get("files", [])
    pending = [f for f in files if (f.get("appProperties") or {}).get("fancamverse_status") != "review_ready"]
    if not pending:
        print("No new videos in FANCAMVERSE/INBOX.")
        return
    for source in pending[:MAX_SOURCES]:
        try:
            print(f"Processing: {source['name']}")
            process_source(service, inbox_id, review_id, processed_id, source, files)
        except Exception as exc:
            print(f"ERROR processing {source.get('name')}: {exc}")
            service.files().update(
                fileId=source["id"],
                body={"appProperties": {"fancamverse_status": "error", "fancamverse_error": str(exc)[:200]}},
                fields="id,appProperties"
            ).execute()
            raise


if __name__ == "__main__":
    main()
