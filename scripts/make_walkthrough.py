"""Build docs/walkthrough.mp4: a narrated walkthrough from the live screenshots.

This is the Loom substitute. It is not a screen recording; it pairs the committed
UI screenshots and two deck slides with macOS text-to-speech narration. The
narration follows docs/walkthrough-script.md.

Requires macOS `say`, `ffmpeg`, and `ffprobe`:

    .venv/bin/python scripts/make_walkthrough.py
"""
from __future__ import annotations

import shutil
import subprocess
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SHOTS = ROOT / "docs" / "screenshots"
OUT = ROOT / "docs" / "walkthrough.mp4"

# (image, narration). Kept to ~4 minutes so it is easy to watch end to end.
SEGMENTS = [
    ("deck-title.png",
     "This is a renewal-risk workbench. Not a dashboard, and not an auto-emailer. "
     "The request was to surface at-risk renewals from five messy systems and email every red account. "
     "I built the detection, the ranking, and the evidence trail, and I pushed back on the auto-send. "
     "The product is a ranked, explainable queue with a recommended play and an approval gate."),
    ("login.jpg",
     "Signing in as Northwind Cloud: five thousand accounts, thirty-six months, about two hundred "
     "eighty-seven thousand raw events. The key resolves to exactly one tenant, and no tenant ID appears "
     "in any URL. The first agent run happened unattended at the frozen as-of, Monday the twenty-sixth of "
     "October, six a.m."),
    ("queue.jpg",
     "This is the Monday brief, computed from the latest successful run, not typed into a slide. The queue "
     "is ranked by calibrated probability times ARR, capped to CSM capacity. A red account with no data "
     "carries a low-confidence badge. Below the tenant's coverage floor it is hard-blocked from email: the "
     "score stays visible, but it cannot trigger outreach. The number at risk is the sum of expected ARR "
     "across red and yellow accounts in the horizon, and every number is a sum over score rows."),
    ("account.jpg",
     "Open the top account and walk it end to end. The probability comes from a versioned, "
     "isotonic-calibrated gradient-boosted model, trained on settled renewals with point-in-time features. "
     "The reasons are occlusion attributions: reset one feature to the training median, and measure the "
     "change in log-odds. They are approximate, and labelled that way. Behind each reason is the record "
     "itself: this failed payment, this ticket, this usage rollup. Lineage walks fact to raw event and "
     "shows both event time and arrival time, so we can prove the Monday run did not use Tuesday's data."),
    ("actions.jpg",
     "The agent drafts; a human decides. Only the kill switch, missing consent, and insufficient source "
     "coverage are hard blocks. An ARR ceiling or a cooldown forces approval, but does not stop a human "
     "from sending. That distinction is deliberate: a guardrail should make me think, not silently override "
     "me. Approving writes three things atomically: the action event, the outbox row with a provider "
     "message ID and the policy trace, and the audit log. Nothing is sent in the UI without a row behind "
     "it. Lumen enables auto-send, but only for the transactional allowlist under consent, coverage, caps, "
     "and ceiling."),
    ("run-detail.jpg",
     "This is the unattended part. A scheduler calls the same run agent as the CLI, with no human trigger: "
     "ingest, score, detect change, route, digest. Re-running the same window is a no-op, and a later run "
     "in the same week cannot create a second action for an account that already has one. Late-arriving "
     "events are held back and picked up by the next run, not backfilled into the past."),
    ("pipeline.jpg",
     "The pipeline screen is the honest view of the data. Two hundred eighty-seven thousand raw events "
     "became two hundred seventy-four thousand facts, with duplicates and unmatched records counted rather "
     "than hidden. Four sources clear the coverage floor; timezone assumptions are stated rather than "
     "inferred silently."),
    ("model.jpg",
     "The split is by renewal date, not by row, so the same contract cannot appear in train and test at "
     "different ages. The test window is genuinely forward in time. Calibration matters more than AUC here, "
     "because the queue multiplies probability by ARR; an overconfident point eight is a real budget error. "
     "The top features are the ones the model actually uses, not the ones that make a nice story."),
    ("deck-scaling.png",
     "Deliberate skips: no causal uplift model, no expansion scoring, no meeting notes, no automated "
     "retraining. Sends default to a simulated provider; the SMTP path exists but is not configured here. "
     "At ten times the data, SQLite full recompute breaks first, then per-account occlusion explanations. "
     "The fix is incremental aggregates, a columnar store, and batched explanations, not a bigger laptop."),
    ("deck-title.png",
     "The one-line version: automate the internal work, make every number traceable, and keep the customer "
     "conversation behind a human until the playbook and consent policy are agreed. That is the product I "
     "would ship first."),
]


def run(cmd: list[str], **kwargs) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, check=True, capture_output=True, text=True, **kwargs)


def duration(path: Path) -> float:
    out = run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
               "-of", "default=nw=1:nk=1", str(path)]).stdout.strip()
    return float(out)


def build() -> Path:
    for tool in ("say", "ffmpeg", "ffprobe"):
        if not shutil.which(tool):
            raise SystemExit(f"{tool} is required to build the walkthrough")

    with tempfile.TemporaryDirectory(prefix="chp-walkthrough-") as tmp:
        tmpdir = Path(tmp)
        clips = []
        for i, (image, narration) in enumerate(SEGMENTS, 1):
            audio = tmpdir / f"seg-{i:02d}.aiff"
            run(["say", "-v", "Samantha", "-r", "170", "-o", str(audio), narration])
            seconds = duration(audio) + 1.0  # 0.4s lead-in, 0.6s tail
            clip = tmpdir / f"seg-{i:02d}.mp4"
            run([
                "ffmpeg", "-y", "-loop", "1", "-i", str(SHOTS / image), "-i", str(audio),
                "-filter_complex",
                "[0:v]scale=1920:1080:force_original_aspect_ratio=decrease,"
                "pad=1920:1080:(ow-iw)/2:(oh-ih)/2:color=0xF7F6F2,setsar=1,format=yuv420p[v];"
                "[1:a]adelay=400|400,apad=pad_dur=0.6[a]",
                "-map", "[v]", "-map", "[a]",
                "-t", f"{seconds:.3f}", "-r", "30",
                "-c:v", "libx264", "-preset", "medium", "-crf", "22",
                "-c:a", "aac", "-b:a", "160k",
                str(clip),
            ])
            clips.append(clip)
            print(f"  {i:02d} {image:<20} {seconds:5.1f}s")

        listing = tmpdir / "clips.txt"
        listing.write_text("".join(f"file '{c}'\n" for c in clips))
        run(["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", str(listing),
             "-c", "copy", str(OUT)])
    return OUT


if __name__ == "__main__":
    path = build()
    print(f"wrote {path} ({duration(path):.1f}s, {path.stat().st_size / 1e6:.1f} MB)")
