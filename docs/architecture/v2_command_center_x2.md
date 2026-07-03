# Command Center X2 Architecture

Command Center X2 is a static local story layer over Command Center X and the
existing OMEGA artifacts. It reads JSON, CSV, and Markdown artifacts; writes
generated HTML/CSS/JS/report files; and does not import app.py, Streamlit,
SQLite, provider APIs, broker clients, or Telegram senders.

X2 differs from X by making the calendar, day detail, strategy story, no-picks,
Learning Foundry, Market Masters, automation, Telegram, RiskHub, and evidence
systems narrative-first instead of table-first.
