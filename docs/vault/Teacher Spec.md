---
tags: [active]
updated: 2026-10-02
---
# Teacher Spec (James's brief, 2026-10-02, kept verbatim in substance)

Add on to x100bot, not a separate bot. A personal photo teacher in the private chat: James sends photos, the teacher says what works, what to improve (composition, light, exposure, focus, colour, the moment) and exactly how next time (where to stand, when, which X100VI settings and how to set them). Follow ups, reshoot and compare, series, ask anything, progress week by week. Replaces x100bot's basic critique handler and prompts/critique_system.md. Reuse config loader, db, ratelimit, telegram, claude wrapper, camera facts, menus, recipe and photographer libraries, light.py, weather.py. Build fully, stop at STOP HERE points.

## Rules that override everything
1. Teach, do not just judge: every note says what to change, why (one sentence principle), how on the X100VI with steps and settings.
2. Biggest problem first: exactly one top fix explained properly, then a few shorter notes. Never twenty problems.
3. Evidence: settings used come only from EXIF. Exposure, tilt, sharpness, colour claims backed by app measurements. Unknown is said, not guessed.
4. Only advice the X100VI can follow: recommended settings are structured fields validated against camera/x100vi_facts.yaml; menu names must exist in camera/menus.yaml; the app prints menu paths (from camera/settings_menu_map.yaml).
5. Private and respectful: only owner_user_id in a private chat. GPS and all metadata stripped before Claude sees a photo. Never identify people, never guess location. Photos stay on the NAS; /forget deletes.
6. Rate limits and a queue: one Claude call at a time app wide, daily caps per call type, usage limit → persistent queue with not_before = reset time.
7. No dashes in any text James reads. Plain warm practical English adapting to level and style.

## What the teacher does
A. Single photo critique. Photo or (better) original JPEG/HEIF as a file (keeps EXIF). Caption = intent. Flags: #quick (short), #settings (readout only, no Claude), #assignment (grade against today's x100bot assignment).
B. Follow ups: reply to any teacher message with a question → answer about that photo, remembering the critique. Buttons: Simpler, More depth, Why these scores, Reshoot plan.
C. Reshoot and compare: reply to a critique with a new photo (or #compare while replying) → before/after: improved, still needs work, new scores.
D. Series: album of 2..series_max_images → strongest and why, weakest, suggested order, what ties them, missing shot.
E. Ask anything: any non command non reply text → question. /plan <what, where, when> → shot plan using x100bot light and forecast for that day.
F. Assignment check: #assignment grades vs today's assignment, marks done in assignments table when met.
G. Progress: every critique scored and tagged → learning profile (recurring issues, strengths), used in later critiques ("third tilted horizon this week"), weekly report with chart, monthly best of. /progress, /best, /history.

## Pipeline for one photo
1. Intake: owner only, private. Job into teacher queue (SQLite). sendChatAction upload_photo refreshed at most every 5 s. If jobs ahead, reply once with queue position.
2. Download: getFile; >20 MB → ask for smaller JPEG. Accept JPEG, HEIF (pillow-heif), PNG. RAF → ask for JPEG/HEIF.
3. EXIF (files only): `exiftool -j -G1`; map model, focal length, aperture, shutter, ISO, exposure comp, exposure mode, metering, focus mode, AF area, flash, shutter type, image size (infer teleconverter from output size), Fujifilm maker notes (film sim, grain, CCE, CCFXB, WB + fine tune, DR, highlight/shadow tone, color, sharpness, NR, clarity). Reuse exif.py. Drop every GPS field, never store or send. Photo (not file) → exif_available false.
4. Measurements (measure.py) on a copy at analysis_long_edge_px, sRGB: highlights_clipped_pct (any channel ≥250), shadows_crushed_pct (luma ≤5), luma mean/median/p5/p95, contrast = luma std; colour: mean a*, b* (CIELAB) over luma 30..220, labelled neutral/warm/cool/green/magenta past cast_ab, mean saturation, oversaturated share; tilt: Canny + probabilistic Hough, lines >15% width within 15° of H or V, length weighted median deviation, tilt_degrees (+ = rotate CCW to level) with confidence, tilted only when ≥tilt_min_lines agree and ≥tilt_min_degrees; sharpness: Laplacian variance on 3x3 grid of 512 px grey copy, sharpest cell name, blur_suspect when sharpest < calibrated threshold. Thresholds in config, calibrated at first STOP HERE.
5. Claude image: long edge claude_long_edge_px, sRGB JPEG, metadata stripped, data/teacher/<critique_id>/photo.jpg.
6. Context (stdin JSON): level, style, detail, caption, exif or exif_available false, measurements, learning profile (tag counts over last 30 critiques, avg scores per area, weakest/strongest), week theme, today's assignment when #assignment, ≤40 recipe names with film sim and light tags (prefer tags fitting measured light), ≤30 photographer names with themes, camera facts, menu names, allowed tags.
7. Claude critique call. 8. Guardrails; retry once with errors appended; then fallback critique.
9. Render overlay at overlay_long_edge_px: thirds grid, crop rectangle if valid, level line when tilt confident. Then guidance.
10. Send: message 1 overlay photo + caption; message 2 reply with full guidance + buttons. Crop preview only on Show crop. Split message 2 once at a section boundary if >4000 chars.
11. Store critique, scores, tags, session id, every teacher message id (reply routing), update profile.

## Claude calls (teacher.py)
Through x100bot claude wrapper and limiter, one at a time, CLAUDE_CODE_MAX_RETRIES from config. Common flags: `--output-format json --permission-mode dontAsk --permission-prompts none --strict-mcp-config --model <teacher.model> --fallback-model <teacher.fallback_model>`. Never --bare with oauth. Context via stdin. Teacher calls KEEP session persistence (no --no-session-persistence). Mount /home/app/.claude as a volume.
1. Critique: cwd data/teacher/<id>/, `claude -p "<teacher brief>" --append-system-prompt-file prompts/teacher_system.md --allowedTools "Read" --disallowedTools "Bash,Edit,Write,Glob,Grep,WebFetch,WebSearch,Agent,NotebookEdit" --max-turns 6 --json-schema prompts/teacher_schema.json`. Store session_id, total_cost_usd.
2. Follow up and buttons: same folder, `--resume <session_id>`, followup brief, --max-turns 4, followup_schema. Button presets: Simpler "Explain the top fix again in simpler words, with fewer terms"; More depth "Go deeper on composition and light, as for an advanced photographer"; Why these scores "Explain each score in one sentence against the rubric"; Reshoot plan "Give me a step by step plan to reshoot this tomorrow". Session older than session_days or resume fails → fresh session with followup_system.md and stored critique JSON in stdin.
3. Compare: copy new Claude copy as after.jpg into original folder, resume with compare_brief.md + compare_schema.json. Fallback: fresh session with compare_system.md reading photo.jpg and after.jpg, earlier critique JSON in stdin.
4. Series: collect album by media_group_id, wait album_debounce_seconds after last part, cap series_max_images, save 1.jpg..N.jpg in a new folder, EXIF and measurements in stdin, series_system.md, --allowedTools "Read", --max-turns 8, series_schema.json.
5. Ask and plan: no tools (`--disallowedTools "Bash,Edit,Write,Read,Glob,Grep,WebFetch,WebSearch,Agent,NotebookEdit"`), ask_system.md + ask_schema.json. Stdin: question, level, profile, camera facts, menu names, recipe and photographer names, for /plan the light times and forecast of that day.
6. Weekly report: no tools, progress_system.md + progress_schema.json. App computes every number; Claude only writes words.
Usage limits: result matches "hit your (session|weekly|Opus|Sonnet) limit" → requeue with not_before = parsed reset time (else +1 h), reply once "teacher is resting until about <time>", scheduler drains queue every 10 min. Opus/Sonnet limit → retry once with fallback model first. The 05:30 plan job has priority: teacher queue pauses while it runs.

## Guardrails (guardrails.py, deterministic)
- Schema match. Scores integers 1..5. Tags and strength tags only from allowed lists.
- Crop: x,y,w,h fractions in 0..1, w and h ≥ 0.35, aspect in 3:2, 4:5, 1:1, 16:9, free. Else drop crop, keep rest.
- Straighten: |value| ≤ 10°, and within 2° of measured tilt when tilt is confident.
- Settings: every non empty field valid for the X100VI. mode P/A/S/M. aperture f/2 up to the smallest aperture the manual lists (confirm). shutter a value the camera offers; faster than 1/4000 only when shutter_type is ES. iso: number 64..51200 or AUTO1/AUTO2/AUTO3 with optional max sensitivity (400..12800) and min shutter (1/2000..30 s or AUTO). exposure_comp −5..+5 in thirds. focus_mode S/C/M. af_area SINGLE POINT, ZONE, WIDE/TRACKING, ALL. nd_filter ON/OFF. teleconverter 35/50/70. film_simulation one of the 20. dynamic_range AUTO/100%/200%/400%. recipe_name empty or exact library name. Empty string = keep.
- Names: learn_from photographer exact library name. No other camera model names.
- Text: no URLs, markdown, emojis, dashes; capitalised words must be in menus.yaml or short forms (ISO, OVF, EVF, ND, IBIS, AF, MF, EV, JPEG, HEIF, RAW, SGT, HDB, MRT).
- No made up EXIF: exif_available false → "used" column prints "unknown"; settings_why must not claim what was used.
- Fallback critique when Claude fails twice: overlay, measured values, EXIF table, advice from library/hints.yaml (rules mapping measurements/EXIF → advice, e.g. highlights_clipped_pct above threshold → exposure comp minus 2/3 or DYNAMIC RANGE 400%; shutter slower than 1/125 with blur_suspect → AUTO2 with MIN. SHUTTER SPEED 1/250; confident tilt → ELECTRONIC LEVEL SETTING on and FRAMING GUIDELINE GRID 9). Mark "Quick check, the full teacher will try again later" and requeue.

## camera/settings_menu_map.yaml
Built from manual pages with URL per entry: where each recommendable setting lives (aperture ring, shutter speed dial, ISO dial, exposure compensation dial and its C position for −5..+5 EV with the front command dial, focus mode selector, SHOOTING SETTING, ISO AUTO SETTING with AUTO1..3 and DEFAULT SENSITIVITY / MAX. SENSITIVITY / MIN. SHUTTER SPEED, AF MODE, ND filter, DIGITAL TELE-CONV., IMAGE QUALITY SETTING items, SCREEN SETTING items such as ELECTRONIC LEVEL SETTING and FRAMING GUIDELINE with GRID 9, GRID 24, HD FRAMING). Renderer prints "How to set it" from this map only.

## Telegram UX (teacher_render.py, bot.py)
Message 1 caption (<1024):
```
<b>{summary}</b>
Composition {c}/5 · Light {l}/5 · Exposure {e}/5
Focus {f}/5 · Colour {co}/5 · Moment {m}/5
Overall {overall}/5{trend_note}
Top fix: <b>{top_fix_title}</b>
{top_fix_why}
```
overall from score_weights, one decimal; trend_note e.g. " (your 30 day average is 3.1)".
Message 2 (reply to 1, buttons Simpler, More depth, Why these scores, Reshoot plan, Show crop when crop exists): sections What I see / What works (• items) / Top fix: title, why, "Why it matters: principle", numbered steps / Composition / Light / When and where to reshoot / Settings `<pre>` table (You used | Try: Mode, Aperture, Shutter, ISO, Exposure comp, Focus, AF area, Film simulation, Dynamic range) + settings_why + "How to set it: <paths>" / Reshoot it like this (numbered) / If you edit / Exercise / "Recipe to try: name, <a>full recipe</a>" / "Learn from: name, why <a>see their work</a>" / "Measured: highlights clipped x%, shadows crushed y%, tilt t°, sharpest area, colour cast" / "Closest recipe to what you shot" (only with Fujifilm EXIF) / question.
Rules: omit empty lines/rows, "keep" in Try when unchanged, "unknown" in You used when no EXIF, never None, escape HTML. Quick mode: message 1 + short message 2 (top fix, settings table, exercise). Follow ups, compares, series: one reply each in thread. Replies to any stored teacher message route to that critique. answerCallbackQuery immediately. callback_data < 64 bytes (action code + critique id).
Style note (James, 2026-10-02): x100bot posts use the global card style; the teacher's layout above is James's spec, keep its structure but it may carry the same emoji header conventions.

## Commands (added)
/teach help; /level beginner|intermediate|advanced; /style encouraging|direct|socratic (socratic opens with two questions); /quick on|off; /plan <what, where, when>; /ask <q> (plain text works); /progress (7 and 30 days); /best (five highest of 30 days as album with scores); /history (last 10, one line each); /forget (reply or id) deletes files, rows, session; /forgetall confirms with a button; /status shows teacher usage and queue.

## Progress (progress.py)
Profile after every critique: tag and strength counts over last 30, avg per area 7 and 30 days, weakest/strongest, most improved vs the 30 days before. Weekly report Sundays weekly_report_cron in private chat: matplotlib line chart PNG of weekly average per area for 8 weeks (clear labels, phone readable, light background), then one message with computed numbers (critiques this week, overall avg and change, top 3 recurring issues, most improved, best photo of week as photo with score) and Claude's words (summary, biggest improvement, focus next week, one exercise, one camera setup change from hints.yaml for the most common issue). Monthly best of at monthly_best_cron: five best of month as album with scores plus one line on what they share.

## Config additions
```
teacher:
  enabled: true
  model: sonnet
  fallback_model: haiku
  level: intermediate
  style: encouraging
  default_detail: full
  timeout_seconds: 300
  session_days: 30
  retention_days: 180
  claude_long_edge_px: 2048
  analysis_long_edge_px: 1024
  overlay_long_edge_px: 1600
  max_file_mb: 20
  series_max_images: 8
  album_debounce_seconds: 3
  weekly_report_cron: "0 20 * * 0"
  monthly_best_cron: "0 20 1 * *"
  score_weights: {composition: 0.25, light: 0.25, moment: 0.15, exposure: 0.15, focus: 0.10, colour: 0.10}
  measurement_thresholds: {highlights_clipped_pct: 1.0, shadows_crushed_pct: 2.0, cast_ab: 6.0, tilt_min_degrees: 0.7, tilt_min_lines: 4, blur_laplacian_min: 0}
limits:
  teacher:
    max_critiques_per_day: 15
    max_followups_per_day: 40
    max_questions_per_day: 20
    max_series_per_day: 3
    queue_max: 20
```
Floors: refuse if max_critiques_per_day > 40, max_followups_per_day > 100, max_questions_per_day > 60, max_series_per_day > 10, series_max_images > 10, queue_max > 50.

## Tables
critiques(id, created_at, kind CHECK IN single/compare/series, source CHECK IN photo/file, file_unique_id, folder, caption, flags_json, exif_json, measurements_json, result_json, scores_json, overall REAL, tags_json, strength_tags_json, session_id, parent_id, model, cost_usd, used_fallback INTEGER, status); teacher_messages(message_id PK, critique_id, role); followups(id, critique_id, at, question, answer_json, cost_usd); teacher_queue(id, kind, payload_json, not_before, attempts, status, created_at); teacher_settings(key PK, value); profile_snapshots(date PK, profile_json).

## Tests
Measurements on Pillow synthetic images (3° horizon detected within 0.5° right sign; level not tilted; blown out passes clipping; warm cast labelled warm; blurred has lower sharpness). EXIF mapping on a fixture JPEG from the X100VI, GPS never in stored or Claude bound data. Guardrails: every invalid setting/crop/straighten fails, valid pass, unknown capitals and other camera models fail, made up used settings rejected when exif_available false. Routing: reply to msg 1 or 2 → follow up; reply with photo → compare; album → one series job; text without reply → ask; non owner ignored. Queue: caps hold; usage limit requeues with not_before; plan job pauses queue; never processed twice. Resume: --resume with stored id, fresh session fallback. Renderer: caption <1024, guidance <4096 or split once at section boundary, table aligned, empty rows omitted, keep/unknown correct. /forget removes files, rows, session. Weekly report numbers match fixture data.

## Build order
1. Read x100bot. Config section + floors, tables, owner only routing, teacher queue, commands skeleton.
2. Intake, download, EXIF, measurements, overlays, synthetic image tests.
3. Critique call, guardrails, settings_menu_map.yaml from the manual, renderer, fallback critique with library/hints.yaml.
4. Follow ups with --resume, buttons, compare, series.
5. Ask and plan, assignment check, progress, weekly report and chart, monthly best of, /forget.
6. Dockerfile (numpy, opencv-python-headless, pillow-heif, matplotlib, volume ./data/claude_home:/home/app/.claude), README section.
STOP HERE after 3: ask James for three photos (one as a file), run them, show critiques with measurements, calibrate thresholds.
STOP HERE after 5: show a weekly report from fixture data including the chart.
Never loosen a limit or remove a guardrail to make a test pass.

Prompt files (verbatim from the brief) are in `prompts/teacher_*`, `followup_*`, `compare_*`, `series_*`, `ask_*`, `progress_*`.
