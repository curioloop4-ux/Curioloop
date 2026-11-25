# FANCAMVERSE Shorts Automation

A separate GitHub Actions pipeline for processing user-owned K-pop fancam footage from Google Drive.

## Flow
1. Upload an original video to the Google Drive `INBOX` folder.
2. Every 15 minutes, GitHub Actions checks for new videos.
3. The processor scores candidate moments using sampled visual motion and audio energy.
4. It renders up to 3 vertical 1080x1920 Shorts per source, preserving the original audio.
5. Draft MP4s and English metadata JSON are uploaded to Drive `REVIEW`.
6. The source is marked processed to prevent duplicate work.

**Publishing is intentionally not automatic yet.** Review each edit and confirm you have the necessary rights to the footage and audio before publishing. YouTube channel OAuth and a publish schedule will be added after the channel is created and connected.

## Google Drive setup
1. In Google Cloud Console, create/select a project and enable **Google Drive API**.
2. Create a service account and download its JSON key.
3. Create a Drive folder named `FANCAMVERSE` and copy its folder ID from the URL.
4. Share that folder with the service account's `client_email` as **Editor**.
5. In GitHub repository **Settings → Secrets and variables → Actions → New repository secret**, add:
   - `GOOGLE_SERVICE_ACCOUNT_JSON`: the complete service-account JSON file contents.
   - `FANCAMVERSE_ROOT_FOLDER_ID`: the Drive folder ID.

The workflow creates `INBOX`, `REVIEW`, and `PROCESSED` subfolders under the root folder.

## Input naming
Recommended filename:
`GROUP__MEMBER__SONG__EVENT.mp4`

Example:
`IVE__WONYOUNG__LOVE_DIVE__SEOUL_CONCERT.mp4`

Optional: add a JSON file with the same basename:
```json
{
  "group": "IVE",
  "member": "Wonyoung",
  "song": "LOVE DIVE",
  "event": "Seoul Concert",
  "rights_confirmed": true
}
```

The processor does not infer or identify idol names from the video. It uses the filename or JSON metadata supplied by the uploader.

## Local run
```bash
pip install -r requirements.txt
export GOOGLE_SERVICE_ACCOUNT_JSON='{"type":"service_account", ...}'
export FANCAMVERSE_ROOT_FOLDER_ID='your-drive-folder-id'
python fancamverse.py
```

## Notes
- Current mode: **render-to-review only**; no public uploads.
- Default output: 3 clips per source, 24 seconds each, 9:16, 1080x1920.
- Cropping is center-based in this initial version. Subject-aware tracking is a later enhancement after testing representative footage.
- A user-recorded fancam is not automatically cleared for redistribution. Music, choreography, event restrictions, performer/label rights, and Content ID may still apply.
