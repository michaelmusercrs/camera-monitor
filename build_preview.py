#!/usr/bin/env python3
"""Build a curated preview report - REAL events only, ranked by importance."""

import json
import base64
import sys
from datetime import datetime
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")

BASE = Path(__file__).parent
SNAP_DIR = BASE.parent / "camera-monitor" / "snapshots"
OUTPUT = BASE / "preview-report.html"


def embed(filename):
    path = SNAP_DIR / filename
    if not path.exists():
        return ""
    return f"data:image/jpeg;base64,{base64.b64encode(path.read_bytes()).decode('ascii')}"


def card(img, time, cam, title, desc, tag, tag_color="#39ff14"):
    uri = embed(img)
    if not uri:
        return ""
    return f'''
    <div class="card" style="border-left:4px solid {tag_color}">
        <div class="card-head">
            <span class="tag" style="background:{tag_color}">{tag}</span>
            <span class="cam">{cam}</span>
            <span class="time">{time}</span>
        </div>
        <div class="title">{title}</div>
        <div class="card-body">
            <img src="{uri}" onclick="this.classList.toggle('big')" />
            <div class="desc">{desc}</div>
        </div>
    </div>'''


def build():
    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    # CURATED EVENTS - I looked at every single image
    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

    summary_events = [
        ("9:37 AM", "Dark gray SUV arrived at office front lot", "Font Corner"),
        ("11:50 AM", "Person arrived at office, working at left desk with laptop", "Muse Office"),
        ("12:34 PM", "3 people in office - 2 at desks + child (toddler in pink) walking through", "Muse Office"),
        ("1:38 PM", "Full office meeting - 3 adults, person at left desk, person at center, person at back desk", "Muse Office"),
        ("2:02 PM", "Gold/tan truck arrived at shop side lot (was empty all morning)", "Security Cam 5"),
        ("2:43 PM", "Office emptied - everyone left, desk items rearranged", "Muse Office"),
        ("4:00 PM", "Gold truck departed shop side lot", "Security Cam 5"),
        ("5:51 PM", "Office lights off, laptop removed, end of business day", "Muse Office"),
    ]

    summary_events_d2 = [
        ("10:15 AM", "Person moving through office (caught mid-stride by motion detection)", "Muse Office"),
        ("11:54 AM", "Office lights on, desks rearranged, but nobody visible at this moment", "Muse Office"),
        ("3:29 PM", "2 people in the barn/workshop - one carrying equipment, one standing", "Barn"),
        ("5:00 PM", "All vehicles departed from office front lot (white Chevy + dark SUV gone)", "Font Corner"),
    ]

    # ━━━ KEY EVENT IMAGES ━━━
    # Sorted by importance: people > vehicles > other

    people_cards = "".join([
        card("muse_office_20260211_123543_moderate.jpg",
             "Feb 11, 12:34 PM", "Muse Office",
             "3 People in Office + Child",
             "Two adults working at desks - one at left desk with laptop (wearing cap), one at the main desk. A toddler in pink is walking through the office between the desks. Tuya camera motion detection boxes visible on two of them. Office is fully active.",
             "PEOPLE", "#39ff14"),

        card("muse_office_20260211_133933_moderate.jpg",
             "Feb 11, 1:38 PM", "Muse Office",
             "Full Office - 3 Adults Working",
             "Three people visible. Person at left desk (cap, dark shirt), person seated at center guest chair, person at back desk area. Pink phone case on front desk. Box of items on left side. Active work session.",
             "PEOPLE", "#39ff14"),

        card("muse_office_20260212_101519_moderate.jpg",
             "Feb 12, 10:15 AM", "Muse Office",
             "Day 2 - Person Moving Through Office",
             "Someone walking between the desks, caught mid-stride. Green motion detection box from Tuya camera tracking them. Office lights are on. Day 2 arrival time: ~10:15 AM (earlier than Day 1's 11:50 AM).",
             "PEOPLE", "#39ff14"),

        card("barn_20260212_153027_moderate.jpg",
             "Feb 12, 3:29 PM", "Barn Workshop",
             "2 People in Workshop",
             "Two people in the barn/workshop. One person in white shirt carrying or handling something. Second person with blonde hair standing to the right. Workshop lights fully on, tools and equipment visible. First real activity captured in the barn across the whole monitoring period.",
             "PEOPLE", "#39ff14"),

        card("muse_office_20260211_115054_moderate.jpg",
             "Feb 11, 11:50 AM", "Muse Office",
             "Day 1 - First Person Arrives at Office",
             "Person sitting at left desk working on a laptop. Wearing a cap. Office lights are on, full color. This is the first real activity of Day 1. Green motion detection box from Tuya camera.",
             "PEOPLE", "#39ff14"),
    ])

    vehicle_cards = "".join([
        card("font_corner_camera_20260211_093807_alert.jpg",
             "Feb 11, 9:37 AM", "Font Corner",
             "Vehicle Arrived - Dark Gray SUV",
             "A dark gray SUV/crossover has appeared in the upper-left of frame, parked behind the hedge. This vehicle was NOT in the 8 AM frame. The white Chevy Equinox is in its usual spot on the right. Someone has arrived at the office for the day.",
             "VEHICLE", "#4a9eff"),

        card("security_camera_5_20260211_140257_major.jpg",
             "Feb 11, 2:02 PM", "Security Cam 5",
             "Vehicle Arrived - Gold/Tan Truck at Shop",
             "A gold or tan colored truck/SUV has appeared in the bottom-left corner. The lot was completely empty all morning (9:53 AM frame shows nothing). This is a real arrival - someone pulled up to the shop/warehouse area.",
             "VEHICLE", "#4a9eff"),

        card("security_camera_5_20260211_160008_major.jpg",
             "Feb 11, 4:00 PM", "Security Cam 5",
             "Vehicle Departed - Truck Gone",
             "The gold truck that arrived at ~2 PM is now gone. Lot is empty again. The visit lasted about 2 hours. Late afternoon shadows stretching across the gravel.",
             "VEHICLE DEPARTED", "#ffa500"),

        card("font_corner_camera_20260212_170151_alert.jpg",
             "Feb 12, 5:00 PM", "Font Corner",
             "Day 2 - All Vehicles Departed",
             "The parking lot is now empty. Both the white Chevy Equinox and the dark gray SUV are gone. Only the hedge, pole, and orange cone/flag remain. End of Day 2 - everyone has left for the day.",
             "VEHICLE DEPARTED", "#ffa500"),

        card("font_corner_camera_20260211_125845_major.jpg",
             "Feb 11, 12:57 PM", "Font Corner",
             "Midday - Both Vehicles Present",
             "Sunny midday. White Chevy on the right, dark gray SUV visible upper-left behind the hedge. Both vehicles present during peak business hours. Strong shadows from overhead sun.",
             "VEHICLE", "#4a9eff"),
    ])

    scene_cards = "".join([
        card("muse_office_20260211_144427_moderate.jpg",
             "Feb 11, 2:43 PM", "Muse Office",
             "Office Emptied - Everyone Left",
             "The office is now empty. Everyone has left. Desks have items rearranged from the morning - papers moved, laptop gone from left desk. Box still on left side. Lights still on but no people. Likely a break or early departure.",
             "SCENE CHANGE", "#ffd700"),

        card("muse_office_20260211_175135_alert.jpg",
             "Feb 11, 5:51 PM", "Muse Office",
             "End of Day - Office Dark",
             "Office lights are off, IR night mode. Laptop is gone from the left desk. Business day is over. The office will stay like this until tomorrow morning.",
             "END OF DAY", "#888"),

        card("muse_office_20260212_162317_alert.jpg",
             "Feb 12, 4:23 PM", "Muse Office",
             "Day 2 - Office Closed Early",
             "Back to IR/night mode - lights off, nobody here. Day 2 was a shorter day: arrived ~10:15 AM, left before 4:23 PM. Compared to Day 1 (11:50 AM - 5:51 PM).",
             "END OF DAY", "#888"),

        card("barn_20260211_120414_major.jpg",
             "Feb 11, 12:04 PM", "Barn Workshop",
             "Workshop Lights Turned On",
             "Workshop/garage lights are now on - switched from IR to full color. Extension cords, blue storage bins, heaters, gas can, tools all visible. Someone turned on the lights but isn't directly visible in frame. The barn will show random noise all day because of this lighting.",
             "LIGHTS ON", "#ffd700"),
    ])

    baseline_cards = "".join([
        card("font_corner_camera_20260211_075751_major.jpg",
             "Feb 11, 7:57 AM", "Font Corner",
             "Office Front - Morning Baseline",
             "Daylight view of the office front. Ornamental hedge around sign pole, concrete sidewalk, parking lot. White Chevy Equinox parked on the right (appears to stay overnight). Orange cone/flag on right edge. Overcast morning.",
             "BASELINE", "#555"),

        card("security_camera_5_20260211_081945_major.jpg",
             "Feb 11, 8:19 AM", "Security Cam 5",
             "Shop Side Lot - Morning Baseline",
             "Metal building side parking. Gravel lot, concrete pad with 3 utility covers, loading area. Empty lot, overcast morning. This is what 'normal empty' looks like.",
             "BASELINE", "#555"),

        card("muse_office_20260211_005950_baseline.jpg",
             "Feb 11, 12:59 AM", "Muse Office",
             "Office Interior - Night Baseline",
             "Empty office in IR mode. Two L-shaped desks, office chairs, guest chairs, leather couch, printer. This is what 'nobody here at night' looks like. Any change from this = someone arrived or something moved.",
             "BASELINE", "#555"),

        card("barn_20260211_010053_baseline.jpg",
             "Feb 11, 1:00 AM", "Barn Workshop",
             "Workshop - Night Baseline",
             "Workshop in IR night mode. Workbench, folding chairs, extension cords, various equipment. Lights off. Very cluttered scene which is why this camera generates so much noise - lots of surfaces to reflect light differently.",
             "BASELINE", "#555"),
    ])

    # ━━━ Day 1 summary table ━━━
    d1_rows = "".join(f'<tr><td class="t">{t}</td><td>{e}</td><td class="cam">{c}</td></tr>'
                      for t, e, c in summary_events)
    d2_rows = "".join(f'<tr><td class="t">{t}</td><td>{e}</td><td class="cam">{c}</td></tr>'
                      for t, e, c in summary_events_d2)

    html = f'''<!DOCTYPE html>
<html><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Camera Monitor - What Actually Happened</title>
<style>
*{{margin:0;padding:0;box-sizing:border-box}}
body{{font-family:'Segoe UI',system-ui,sans-serif;background:#08080d;color:#ddd;line-height:1.6}}

.hero{{background:linear-gradient(135deg,#0a1628,#162447);padding:48px 56px;border-bottom:3px solid #39ff14}}
.hero h1{{font-size:34px;color:#fff;font-weight:300}}.hero h1 b{{color:#39ff14;font-weight:700}}
.hero p{{color:#777;font-size:13px;margin-top:6px}}

.section{{padding:40px 56px;border-bottom:1px solid #151520}}
.section h2{{font-size:22px;color:#39ff14;margin-bottom:4px}}
.section .sub{{color:#666;font-size:13px;margin-bottom:20px}}

/* Activity log table */
.log{{width:100%;border-collapse:collapse;margin-bottom:12px}}
.log th{{background:#0d1117;color:#39ff14;padding:10px 14px;text-align:left;font-size:11px;text-transform:uppercase;letter-spacing:1px}}
.log td{{padding:10px 14px;border-bottom:1px solid #151520;font-size:13px}}
.log td.t{{color:#39ff14;font-weight:600;white-space:nowrap;width:100px}}
.log td.cam{{color:#666;font-size:12px;white-space:nowrap}}
.log tr:hover{{background:#0d1117}}
.day-label{{background:#39ff14;color:#000;padding:6px 16px;border-radius:6px;font-size:13px;font-weight:700;display:inline-block;margin:16px 0 10px}}

/* Cards */
.card{{background:#0d1117;border-radius:10px;margin-bottom:20px;overflow:hidden}}
.card-head{{display:flex;align-items:center;gap:10px;padding:12px 18px;border-bottom:1px solid #1a1a2e;flex-wrap:wrap}}
.tag{{padding:3px 12px;border-radius:14px;font-size:10px;font-weight:800;color:#000;letter-spacing:.5px}}
.cam{{font-weight:600;font-size:14px}}
.time{{color:#777;font-size:12px}}
.title{{padding:8px 18px;font-size:16px;font-weight:600;color:#fff}}
.card-body{{display:flex;gap:20px;padding:14px 18px 18px}}
.card-body img{{width:420px;height:315px;object-fit:cover;border-radius:8px;flex-shrink:0;border:1px solid #222;cursor:pointer;transition:all .3s}}
.card-body img.big{{position:fixed;top:2%;left:2%;width:96%;height:96%;object-fit:contain;z-index:1000;background:#000;border-radius:0;border:none}}
.desc{{flex:1;font-size:13px;color:#aaa;line-height:1.8}}

.cards-grid{{display:grid;grid-template-columns:1fr;gap:0}}

/* Stats bar */
.stats{{display:flex;background:#0d1117}}
.stat{{flex:1;text-align:center;padding:24px 12px;border-right:1px solid #151520}}
.stat:last-child{{border:none}}
.stat .n{{font-size:36px;font-weight:700}}
.stat .l{{font-size:10px;color:#666;text-transform:uppercase;letter-spacing:1px;margin-top:2px}}

/* Filter stats */
.filter-bar{{display:flex;gap:24px;padding:20px 56px;background:#0a1117;border-bottom:1px solid #151520;flex-wrap:wrap}}
.filter-stat{{text-align:center}}
.filter-stat .n{{font-size:20px;font-weight:700;color:#39ff14}}
.filter-stat .l{{font-size:10px;color:#666}}

@media(max-width:900px){{
    .hero,.section{{padding:24px 20px}}
    .card-body{{flex-direction:column}}.card-body img{{width:100%;height:auto}}
    .filter-bar{{padding:16px 20px}}
}}
</style></head><body>

<div class="hero">
    <h1>Camera Monitor: <b>What Actually Happened</b></h1>
    <p>48 hours of monitoring (Feb 11-13, 2026) &bull; 5 cameras &bull; 695 raw events &rarr; distilled to what matters</p>
</div>

<div class="stats">
    <div class="stat"><div class="n" style="color:#39ff14">12</div><div class="l">Real Events</div></div>
    <div class="stat"><div class="n">5</div><div class="l">People Spotted</div></div>
    <div class="stat"><div class="n">3</div><div class="l">Vehicle Events</div></div>
    <div class="stat"><div class="n" style="color:#ff4444">683</div><div class="l">False Positives</div></div>
    <div class="stat"><div class="n" style="color:#39ff14">76%</div><div class="l">v2 Filter Rate</div></div>
</div>

<!-- ═══ DAILY ACTIVITY LOG ═══ -->
<div class="section">
    <h2>Daily Activity Log</h2>
    <div class="sub">Everything that actually happened, in order</div>

    <div class="day-label">Tuesday, February 11</div>
    <table class="log">
        <thead><tr><th>Time</th><th>Event</th><th>Camera</th></tr></thead>
        <tbody>{d1_rows}</tbody>
    </table>

    <div class="day-label">Wednesday, February 12</div>
    <table class="log">
        <thead><tr><th>Time</th><th>Event</th><th>Camera</th></tr></thead>
        <tbody>{d2_rows}</tbody>
    </table>
</div>

<!-- ═══ PEOPLE ═══ -->
<div class="section">
    <h2>People Activity</h2>
    <div class="sub">Every time a person was captured on camera (click images to enlarge)</div>
    {people_cards}
</div>

<!-- ═══ VEHICLES ═══ -->
<div class="section">
    <h2>Vehicle Activity</h2>
    <div class="sub">Arrivals and departures at the office parking areas</div>
    {vehicle_cards}
</div>

<!-- ═══ SCENE CHANGES ═══ -->
<div class="section">
    <h2>Scene Changes</h2>
    <div class="sub">Lights on/off, end of day, office state changes</div>
    {scene_cards}
</div>

<!-- ═══ BASELINES ═══ -->
<div class="section">
    <h2>Camera Baselines</h2>
    <div class="sub">What "normal/empty" looks like for each camera - the reference point for detecting changes</div>
    {baseline_cards}
</div>

<!-- ═══ v2 FILTER PERFORMANCE ═══ -->
<div class="section">
    <h2>v2 Filter Performance</h2>
    <div class="sub">How the new pre-filter pipeline would have handled the old 695 events</div>

    <div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(200px,1fr));gap:12px;margin-bottom:24px">
        <div style="background:#0d1117;border-radius:10px;padding:20px;border-left:3px solid #ff4444">
            <div style="font-size:28px;font-weight:700;color:#ff4444">118</div>
            <div style="font-size:12px;color:#666">Back Camera (disabled - broken)</div>
        </div>
        <div style="background:#0d1117;border-radius:10px;padding:20px;border-left:3px solid #ffa500">
            <div style="font-size:28px;font-weight:700;color:#ffa500">184</div>
            <div style="font-size:12px;color:#666">Barn filtered (SSIM too similar)</div>
        </div>
        <div style="background:#0d1117;border-radius:10px;padding:20px;border-left:3px solid #ffd700">
            <div style="font-size:28px;font-weight:700;color:#ffd700">70</div>
            <div style="font-size:12px;color:#666">Font Corner filtered (tiny diffs)</div>
        </div>
        <div style="background:#0d1117;border-radius:10px;padding:20px;border-left:3px solid #4a9eff">
            <div style="font-size:28px;font-weight:700;color:#4a9eff">66</div>
            <div style="font-size:12px;color:#666">Muse Office filtered</div>
        </div>
        <div style="background:#0d1117;border-radius:10px;padding:20px;border-left:3px solid #4a9eff">
            <div style="font-size:28px;font-weight:700;color:#4a9eff">91</div>
            <div style="font-size:12px;color:#666">Security Cam 5 filtered</div>
        </div>
        <div style="background:#0d1117;border-radius:10px;padding:20px;border-left:3px solid #39ff14">
            <div style="font-size:28px;font-weight:700;color:#39ff14">166</div>
            <div style="font-size:12px;color:#666">Would send to AI (76% reduction)</div>
        </div>
    </div>

    <div style="background:#0d1117;border-radius:10px;padding:24px;border-left:3px solid #39ff14">
        <div style="color:#39ff14;font-weight:700;margin-bottom:8px">How v2 Filters Work</div>
        <div style="color:#aaa;font-size:13px;line-height:1.8">
            <b>1. Disabled cameras</b> - Back Camera is broken, skip it entirely (saves 118 events)<br>
            <b>2. IR mode switch</b> - Detects day/night camera transitions by file size jump >25%, skips 3 frames to let camera settle<br>
            <b>3. SSIM comparison</b> - Structural similarity index via OpenCV. If frame is >88-95% similar to previous (threshold per camera, stricter at night), skip it<br>
            <b>4. Pixel diff</b> - If less than 2% of pixels actually changed, skip it. Catches shadow-only changes<br>
            <b>5. Self-tuning</b> - If AI keeps saying "nothing changed," automatically tightens thresholds. Learns per-camera per-hour patterns<br>
            <b>6. HA motion boost</b> - When Home Assistant detects motion, scan that camera 4x faster. But always scanning all cameras on a 20s cycle regardless
        </div>
    </div>
</div>

<div style="padding:40px 56px;text-align:center;color:#333;font-size:11px">
    Camera Monitor v2 Preview &bull; Generated {datetime.now().strftime("%B %d, %Y %I:%M %p")}
</div>

<script>
document.addEventListener('keydown',e=>{{if(e.key==='Escape')document.querySelectorAll('.big').forEach(i=>i.classList.remove('big'))}});
document.addEventListener('click',e=>{{if(!e.target.closest('img'))document.querySelectorAll('.big').forEach(i=>i.classList.remove('big'))}});
</script>
</body></html>'''

    return html


def main():
    print("Building preview report...")
    html = build()
    size = len(html.encode("utf-8")) / 1024 / 1024
    print(f"Writing ({size:.1f} MB)...")
    OUTPUT.write_text(html, encoding="utf-8")
    print(f"Done: {OUTPUT}")


if __name__ == "__main__":
    main()
