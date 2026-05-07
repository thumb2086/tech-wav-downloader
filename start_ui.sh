#!/usr/bin/env bash
cd /home/thumb/tech_wav_project
source .venv/bin/activate 2>/dev/null || true
streamlit run ui/app.py --server.port 8502 --server.headless true