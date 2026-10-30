"""Generate docs/solution-brief.pptx from the live model artifact.

Run after scripts/bootstrap.py:
    .venv/bin/python scripts/make_deck.py
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from pptx import Presentation
from pptx.dml.color import RGBColor
from pptx.enum.shapes import MSO_SHAPE
from pptx.enum.text import PP_ALIGN
from pptx.util import Inches, Pt

ROOT = Path(__file__).resolve().parent.parent
DB = ROOT / "data" / "chp.db"

INK = RGBColor(0x14, 0x1B, 0x2D)
MUTED = RGBColor(0x5B, 0x67, 0x7A)
PAPER = RGBColor(0xF7, 0xF6, 0xF2)
WHITE = RGBColor(0xFF, 0xFF, 0xFF)
TEAL = RGBColor(0x0F, 0x76, 0x6E)
ORANGE = RGBColor(0xC2, 0x5B, 0x1A)
RED = RGBColor(0xA8, 0x2B, 0x2B)
LINE = RGBColor(0xD9, 0xD5, 0xCB)


def _metrics() -> dict:
    if not DB.exists():
        return {}
    conn = sqlite3.connect(DB)
    try:
        row = conn.execute("""SELECT id, metrics_json FROM model_versions
                              WHERE tenant_id='tnt_northwind' AND is_active=1
                              ORDER BY trained_at DESC LIMIT 1""").fetchone()
        if not row:
            return {}
        m = json.loads(row[1])
        m["version"] = row[0]
        latest = conn.execute("""SELECT id FROM agent_runs WHERE tenant_id='tnt_northwind'
                                 AND status='success' ORDER BY started_at DESC LIMIT 1""").fetchone()
        if latest:
            m["_bands"] = dict(conn.execute("""SELECT band, COUNT(*) FROM scores
                                               WHERE tenant_id='tnt_northwind' AND run_id=?
                                               GROUP BY band""", (latest[0],)).fetchall())
            m["_risk_arr"] = conn.execute("""SELECT COALESCE(SUM(expected_arr_at_risk_cents),0)
                                              FROM scores WHERE tenant_id='tnt_northwind' AND run_id=?
                                              AND band IN ('red','yellow')
                                              AND json_extract(reason_json, '$.in_horizon') = 1""", (latest[0],)).fetchone()[0]
        return m
    finally:
        conn.close()


def _bg(slide, color=PAPER):
    fill = slide.background.fill
    fill.solid()
    fill.fore_color.rgb = color


def _text(slide, x, y, w, h, text, size=18, color=INK, bold=False, align=PP_ALIGN.LEFT,
          font="Avenir Next", spacing=1.0):
    box = slide.shapes.add_textbox(Inches(x), Inches(y), Inches(w), Inches(h))
    tf = box.text_frame
    tf.word_wrap = True
    tf.margin_left = tf.margin_right = 0
    tf.margin_top = tf.margin_bottom = 0
    p = tf.paragraphs[0]
    p.alignment = align
    p.line_spacing = spacing
    run = p.add_run()
    run.text = text
    run.font.size = Pt(size)
    run.font.bold = bold
    run.font.color.rgb = color
    run.font.name = font
    return box


def _bullets(slide, x, y, w, h, items, size=17, color=INK, bullet_color=TEAL, gap=8):
    box = slide.shapes.add_textbox(Inches(x), Inches(y), Inches(w), Inches(h))
    tf = box.text_frame
    tf.word_wrap = True
    tf.margin_left = tf.margin_right = 0
    tf.margin_top = tf.margin_bottom = 0
    for i, item in enumerate(items):
        p = tf.paragraphs[0] if i == 0 else tf.add_paragraph()
        p.space_after = Pt(gap)
        p.line_spacing = 1.12
        dot = p.add_run()
        dot.text = "●  "
        dot.font.size = Pt(size * 0.62)
        dot.font.color.rgb = bullet_color
        dot.font.name = "Avenir Next"
        run = p.add_run()
        run.text = item
        run.font.size = Pt(size)
        run.font.color.rgb = color
        run.font.name = "Avenir Next"
    return box


def _kicker(slide, text, color=TEAL):
    _text(slide, 0.8, 0.55, 11.5, 0.35, text.upper(), size=11, color=color, bold=True)


def _title(slide, text, size=34, y=0.95):
    _text(slide, 0.8, y, 11.8, 1.0, text, size=size, bold=True, color=INK)


def _rule(slide, y):
    line = slide.shapes.add_shape(MSO_SHAPE.RECTANGLE, Inches(0.8), Inches(y), Inches(11.7), Inches(0.02))
    line.fill.solid()
    line.fill.fore_color.rgb = LINE
    line.line.fill.background()


def _card(slide, x, y, w, h, label, value, detail="", accent=TEAL):
    shape = slide.shapes.add_shape(MSO_SHAPE.ROUNDED_RECTANGLE, Inches(x), Inches(y), Inches(w), Inches(h))
    shape.fill.solid()
    shape.fill.fore_color.rgb = WHITE
    shape.line.color.rgb = LINE
    shape.line.width = Pt(0.75)
    shape.adjustments[0] = 0.08
    _text(slide, x + 0.2, y + 0.18, w - 0.4, 0.28, label.upper(), size=9.5, color=MUTED, bold=True)
    _text(slide, x + 0.2, y + 0.48, w - 0.4, 0.55, value, size=27, color=accent, bold=True)
    if detail:
        _text(slide, x + 0.2, y + 1.02, w - 0.4, 0.4, detail, size=10.5, color=MUTED)


def _notes(slide, text):
    slide.notes_slide.notes_text_frame.text = text


def _fmt_pct(v, digits=0):
    return f"{float(v) * 100:.{digits}f}%"


def build() -> Path:
    m = _metrics()
    at90 = m.get("at_90d", {})
    queue = m.get("queue", {})
    thresholds = m.get("thresholds", {})
    split = m.get("split", {})
    bands = m.get("_bands", {})
    risk_arr = m.get("_risk_arr", 0)

    prs = Presentation()
    prs.slide_width = Inches(13.333)
    prs.slide_height = Inches(7.5)
    blank = prs.slide_layouts[6]

    # ---------------------------------------------------------------- 1 title
    s = prs.slides.add_slide(blank)
    _bg(s, INK)
    bar = s.shapes.add_shape(MSO_SHAPE.RECTANGLE, Inches(0.8), Inches(1.55), Inches(0.14), Inches(1.9))
    bar.fill.solid()
    bar.fill.fore_color.rgb = ORANGE
    bar.line.fill.background()
    _text(s, 1.15, 1.35, 11, 0.4, "SOLUTION BRIEF  ·  CUSTOMER SUCCESS PLATFORM", size=12, color=RGBColor(0xB9, 0xC2, 0xD0), bold=True)
    _text(s, 1.15, 1.8, 11, 1.2, "Renewal Risk Workbench", size=46, color=WHITE, bold=True)
    _text(s, 1.15, 3.0, 10.8, 1.1,
          "A ranked, explainable, approval-gated answer to one question:\n"
          "which conversations protect the most revenue this week?",
          size=22, color=RGBColor(0xE6, 0xE2, 0xD8), spacing=1.15)
    _text(s, 1.15, 6.35, 11, 0.35,
          "Prepared for the VP, Customer Success  ·  Working demo + repository + model card",
          size=11.5, color=RGBColor(0x98, 0xA2, 0xB2))
    _notes(s, "Open by naming the real job: a finite, ranked, explainable list, not another dashboard. "
              "The system is a workbench, not an auto-emailer.")

    # ---------------------------------------------------------------- 2 problem
    s = prs.slides.add_slide(blank)
    _bg(s)
    _kicker(s, "The problem")
    _title(s, "The request assumes detection is the hard part. It isn't.")
    _rule(s, 2.0)
    _bullets(s, 0.8, 2.35, 5.7, 3.9, [
        "Five systems already emit the signals. Nobody can triangulate them by hand across 120 accounts.",
        "A red/yellow/green column adds a sixth thing to read on Monday; it does not create a reason to open the account.",
        "The two lost renewals connected a usage decline in March, a single-threaded relationship in May, and an August renewal — too late.",
        "The missing artefact is a finite queue with an owner, a reason, and a next action.",
    ], size=16.5)
    quote = s.shapes.add_shape(MSO_SHAPE.ROUNDED_RECTANGLE, Inches(7.0), Inches(2.4), Inches(5.4), Inches(3.5))
    quote.fill.solid()
    quote.fill.fore_color.rgb = INK
    quote.line.fill.background()
    quote.adjustments[0] = 0.05
    _text(s, 7.35, 2.75, 4.7, 0.3, "THE JOB TO BE DONE", size=10.5, color=RGBColor(0x9F, 0xB6, 0xB2), bold=True)
    _text(s, 7.35, 3.15, 4.7, 2.3,
          "“Of the ~120 accounts I own, which 8 conversations will protect the most revenue this week — and what exactly should I say?”",
          size=20, color=WHITE, spacing=1.15)
    _notes(s, "Do not accept the framing that a dashboard or an alert is the deliverable. "
              "The queue, the reason, and the play are the deliverable.")

    # ---------------------------------------------------------------- 3 build first
    s = prs.slides.add_slide(blank)
    _bg(s)
    _kicker(s, "What I would build first")
    _title(s, "Four parts. Trust the first one before automating the last.")
    _rule(s, 2.0)
    items = [
        ("01", "A fitted model", "Calibrated P(churn at next renewal) from point-in-time features. Gradient-boosted trees, temporal validation, isotonic calibration. Not an LLM judgement."),
        ("02", "A ranked queue", "P × ARR, bounded by CSM capacity. Top reasons, the raw records behind them, a coverage badge, and a recommended play."),
        ("03", "An agent", "Nightly ingest, rescore, change detection, routing, drafts, and a Monday brief. Runs with no human trigger."),
        ("04", "Lineage", "Run → model version → feature value → evidence → raw record, with event time and arrival time."),
    ]
    for i, (num, head, body) in enumerate(items):
        x = 0.8 + (i % 2) * 6.05
        y = 2.35 + (i // 2) * 2.15
        card = s.shapes.add_shape(MSO_SHAPE.ROUNDED_RECTANGLE, Inches(x), Inches(y), Inches(5.75), Inches(1.85))
        card.fill.solid()
        card.fill.fore_color.rgb = WHITE
        card.line.color.rgb = LINE
        card.line.width = Pt(0.75)
        card.adjustments[0] = 0.06
        _text(s, x + 0.25, y + 0.16, 0.6, 0.4, num, size=13, color=ORANGE, bold=True)
        _text(s, x + 0.8, y + 0.14, 4.7, 0.4, head, size=16.5, bold=True)
        _text(s, x + 0.8, y + 0.62, 4.7, 1.1, body, size=11.5, color=MUTED, spacing=1.1)
    _notes(s, "Sequence matters: weeks 1–3 are the model, queue and evidence trail for one tenant, with no outreach automation. "
              "Approval-gated drafts come after the numbers are trusted.")

    # ---------------------------------------------------------------- 4 disagreement
    s = prs.slides.add_slide(blank)
    _bg(s)
    _kicker(s, "Where I disagree", ORANGE)
    _title(s, "Automate the internal work. Gate the customer conversation.")
    _rule(s, 2.0)
    _text(s, 0.8, 2.3, 5.7, 0.4, "AUTOMATE", size=11.5, color=TEAL, bold=True)
    _bullets(s, 0.8, 2.7, 5.6, 3.2, [
        "Ingest, resolve, deduplicate, and hold back late data.",
        "Score every account; detect band changes and risk jumps.",
        "Build the queue, tasks, drafts, and the Monday brief.",
        "Enforce consent, caps, cooldowns, and audit every send.",
    ], size=15.5)
    _text(s, 7.0, 2.3, 5.4, 0.4, "DO NOT AUTO-SEND ON RED", size=11.5, color=RED, bold=True)
    _bullets(s, 7.0, 2.7, 5.4, 3.2, [
        "A risk score is a statement about our uncertainty, not a fact about the customer.",
        "Telling a fragile account we think they are leaving leaks the model and has no theory of change.",
        "Consent, CAN-SPAM/GDPR, and frequency caps are not model features.",
        "Exception: transactional, allowlisted plays (e.g. dunning) under ARR/consent/cap guardrails.",
    ], size=15.5, bullet_color=RED)
    _text(s, 0.8, 6.5, 11.8, 0.4,
          "Default tenant policy: approval required. Lumen enables transactional auto-send only to exercise the guardrail path.",
          size=11, color=MUTED)
    _notes(s, "This is the main disagreement with the request. The system makes the human faster and better informed; "
              "it does not pretend a churn probability is a customer message.")

    # ---------------------------------------------------------------- 5 architecture
    s = prs.slides.add_slide(blank)
    _bg(s)
    _kicker(s, "How it works")
    _title(s, "Messy sources in. A defensible queue out.")
    _rule(s, 2.0)
    stages = [
        ("5 SOURCES", "Salesforce\nStripe\nZendesk\nGmail\nProduct"),
        ("RAW", "Immutable\narrival time\nnatural keys"),
        ("FACTS", "Typed, resolved\nconsent, renewals\nquality issues"),
        ("FEATURES", "Point-in-time\nmonthly panel\n44 signals"),
        ("MODEL", "GBM +\nisotonic\ncalibration"),
        ("WORK", "Ranked queue\nreasons + plays\napproval"),
    ]
    x0, y0, w, h, gap = 0.8, 2.55, 1.78, 1.55, 0.16
    for i, (head, body) in enumerate(stages):
        x = x0 + i * (w + gap)
        box = s.shapes.add_shape(MSO_SHAPE.ROUNDED_RECTANGLE, Inches(x), Inches(y0), Inches(w), Inches(h))
        box.fill.solid()
        box.fill.fore_color.rgb = INK if i in (2, 4, 5) else WHITE
        box.line.color.rgb = LINE if i not in (2, 4, 5) else INK
        box.adjustments[0] = 0.08
        _text(s, x + 0.14, y0 + 0.14, w - 0.28, 0.3, head, size=10, bold=True,
              color=TEAL if i in (2, 4, 5) else MUTED)
        _text(s, x + 0.14, y0 + 0.52, w - 0.28, 0.9, body, size=10.5,
              color=WHITE if i in (2, 4, 5) else INK, spacing=1.12)
        if i < len(stages) - 1:
            arrow = s.shapes.add_shape(MSO_SHAPE.RIGHT_ARROW, Inches(x + w + 0.02), Inches(y0 + 0.62),
                                       Inches(0.12), Inches(0.28))
            arrow.fill.solid()
            arrow.fill.fore_color.rgb = LINE
            arrow.line.fill.background()
    _text(s, 0.8, 4.55, 5.6, 0.4, "TWO INVARIANTS", size=11, color=ORANGE, bold=True)
    _bullets(s, 0.8, 4.95, 5.7, 1.4, [
        "Point-in-time: occurred_at ≤ as_of AND arrived_at ≤ as_of.",
        "Lineage: every fact has a raw_event_id; every score stores its feature vector and hash.",
    ], size=13, gap=4)
    _text(s, 7.0, 4.55, 5.4, 0.4, "TENANCY", size=11, color=ORANGE, bold=True)
    _bullets(s, 7.0, 4.95, 5.4, 1.4, [
        "Bearer key → exactly one tenant; no tenant ID in URLs.",
        "Every table and query is tenant-scoped; cross-tenant IDs return 404.",
    ], size=13, gap=4)
    _notes(s, "Walk left to right. The two invariants are the trust story: no time travel, no untraceable numbers. "
              "Tenancy is resolved from the token, not the request.")

    # ---------------------------------------------------------------- 6 evidence
    s = prs.slides.add_slide(blank)
    _bg(s)
    _kicker(s, "Evidence")
    _title(s, "The model is fitted and calibrated, not prompted.")
    _rule(s, 2.0)
    cards = [
        ("ROC-AUC", f"{m.get('auc', 0):.3f}", "all horizons, held-out renewals"),
        ("AUC @ 90 days", f"{at90.get('auc', 0):.3f}", "operational horizon"),
        ("PR-AUC", f"{m.get('pr_auc', 0):.3f}", f"base rate {m.get('base_rate', 0):.1%}"),
        ("Brier", f"{m.get('brier', 0):.3f}", "calibrated probabilities"),
        ("ECE", f"{m.get('ece', 0):.3f}", "calibration error"),
        ("Top-100 precision", f"{queue.get('precision_at_k', 0):.2f}", "churn at 90 days"),
    ]
    for i, (label, value, detail) in enumerate(cards):
        _card(s, 0.8 + (i % 3) * 4.0, 2.3 + (i // 3) * 1.55, 3.7, 1.35, label, value, detail)
    _text(s, 0.8, 5.55, 11.8, 1.2,
          f"Temporal split by renewal date: train through {split.get('train_through', '—')[:10]}, "
          f"calibrate to {split.get('calib_through', '—')[:10]}, test from {split.get('test_from', '—')[:10]}. "
          f"Bands: red ≥ {thresholds.get('red', 0):.2f}, yellow ≥ {thresholds.get('yellow', 0):.2f}. "
          "Explanations are occlusion attribution (reset one feature to the training median), approximate and labelled as such.",
          size=11.5, color=MUTED, spacing=1.15)
    _text(s, 0.8, 6.75, 11.8, 0.3,
          f"Active model {m.get('version', '—')}  ·  {sum(bands.values()):,} accounts scored in the latest run  ·  "
          f"${risk_arr/100:,.0f} expected ARR at risk in horizon (red + yellow)",
          size=10.5, color=MUTED)
    _notes(s, "Do not lead with AUC. Lead with calibration: the queue multiplies probability by ARR, so overconfidence is a budget error. "
              "The split is by renewal date, not by row, so the same contract cannot leak across train and test.")

    # ---------------------------------------------------------------- 7 guardrails
    s = prs.slides.add_slide(blank)
    _bg(s)
    _kicker(s, "Guardrails")
    _title(s, "Policy is code, and every rule is logged.")
    _rule(s, 2.0)
    cols = [
        ("HARD BLOCK", RED, ["Tenant kill switch", "Missing email consent / opt-out", "Source coverage below tenant minimum"]),
        ("FORCES APPROVAL", ORANGE, ["ARR above auto-send ceiling", "Cooldown or daily/monthly cap", "Open P1 incident", "Any non-transactional play"]),
        ("CAN AUTO-SEND", TEAL, ["Transactional allowlist only", "Consent + coverage + caps + ceiling all pass", "Kill switch off"]),
    ]
    for i, (head, color, items) in enumerate(cols):
        x = 0.8 + i * 4.0
        _text(s, x, 2.35, 3.6, 0.35, head, size=11, color=color, bold=True)
        _bullets(s, x, 2.8, 3.6, 2.8, items, size=13.5, bullet_color=color, gap=6)
    _rule(s, 5.65)
    _text(s, 0.8, 5.9, 11.8, 1.2,
          "One action per account per play per ISO week, and one open action per account per cooldown. "
          "Re-running the same window is a no-op. Approval writes an action event, an outbox row with a provider ID, "
          "and an audit record in one transaction — so “sent” always has a row behind it.",
          size=12.5, color=INK, spacing=1.15)
    _notes(s, "The important distinction is hard block vs approval-forcing. A guardrail should make a human think, "
              "not silently override a human decision. Transactional dunning is the only auto-send exception.")

    # ---------------------------------------------------------------- 8 asks / 10x
    s = prs.slides.add_slide(blank)
    _bg(s, INK)
    _text(s, 0.8, 0.6, 11.8, 0.35, "WHAT I NEED  /  WHAT BREAKS", size=11, color=RGBColor(0x9F, 0xB6, 0xB2), bold=True)
    _text(s, 0.8, 1.05, 11.8, 0.8, "Decisions before code, and an honest scaling order.", size=31, color=WHITE, bold=True)
    _text(s, 0.8, 2.25, 5.7, 0.35, "FROM THE BUSINESS", size=11, color=ORANGE, bold=True)
    _bullets(s, 0.8, 2.7, 5.7, 3.8, [
        "Clean renewal date, ARR, term — not inferred from opportunity stage.",
        "A churn definition and a settled-outcome window.",
        "Consent and contact policy, including who may send.",
        "The playbook, owners, SLAs, and a capacity number.",
        "Acted / renewed feedback, or the model cannot be retrained honestly.",
    ], size=13.5, color=RGBColor(0xE6, 0xE2, 0xD8), bullet_color=ORANGE, gap=7)
    _text(s, 7.0, 2.25, 5.4, 0.35, "AT 10× (600K ACCOUNTS / 20M EVENTS)", size=11, color=TEAL, bold=True)
    _bullets(s, 7.0, 2.7, 5.4, 3.8, [
        "1. SQLite nightly full recompute.",
        "2. Per-account occlusion explanations.",
        "3. Queue partitioning by CSM pod.",
        "Fix: incremental aggregates, columnar store, batched explanations.",
        "Do not start by buying a bigger laptop.",
    ], size=13.5, color=RGBColor(0xE6, 0xE2, 0xD8), bullet_color=TEAL, gap=7)
    _text(s, 0.8, 6.85, 11.8, 0.3,
          "Demo: Northwind Cloud (5,000 accounts) and Lumen Analytics (600)  ·  tests cover point-in-time, tenancy, policy, idempotency, traceability.",
          size=10, color=RGBColor(0x98, 0xA2, 0xB2))
    _notes(s, "Close on the business inputs and the scaling order. The point is not that the demo is production-scale; "
              "it is that the first failure is known and the migration path is named.")

    out = ROOT / "docs" / "solution-brief.pptx"
    prs.save(out)
    return out


if __name__ == "__main__":
    path = build()
    print(f"wrote {path}")
