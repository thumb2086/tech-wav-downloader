import streamlit as st
import sys, time, subprocess
sys.path.insert(0, "/home/thumb/tech_wav_project")
from modules.podcast_manager import PodcastManager

st.set_page_config(page_title="科技浪 Podcast 字幕下載器", layout="wide", page_icon="🎙️")

SUBTITLE_DIR = "/home/thumb/hermes-memory-skills/project-notes/tech_wav_eps/subtitles/"

if "pm" not in st.session_state:
    st.session_state.pm = PodcastManager()
if "start_clicked" not in st.session_state:
    st.session_state.start_clicked = False
if "worker_count" not in st.session_state:
    st.session_state.worker_count = 3
if "ep_filter_key" not in st.session_state:
    st.session_state.ep_filter_key = "全部"

pm = st.session_state.pm

# Handle start click
if st.session_state.start_clicked:
    st.session_state.start_clicked = False
    if not pm.is_downloading:
        pm.start_download()
        st.rerun()

# ── Sidebar ──
with st.sidebar:
    st.title("🎙️ 科技浪")
    st.caption("Podcast 字幕下載管理器")

    # Worker count slider
    max_workers = st.slider("⚡ 同時下載線程數", min_value=1, max_value=20, value=st.session_state.worker_count)
    st.session_state.worker_count = max_workers
    pm.MAX_WORKERS = max_workers

    summary = pm.get_summary()
    col1, col2 = st.columns(2)
    col1.metric("總集數", summary["total"])
    col2.metric("已下載", summary["done"])
    col1.metric("剩餘", summary["remaining"])
    col2.metric("無字幕", summary["no_subs"])
    st.divider()

    is_dling = pm.is_downloading
    if is_dling:
        st.success("⏳ 正在下載中...")
        if st.button("⏹️ 停止下載", use_container_width=True):
            pm.stop_download()
            st.rerun()
    else:
        btn_disabled = summary["remaining"] == 0
        if st.button("▶️ 開始下載字幕", type="primary", use_container_width=True,
                     disabled=btn_disabled):
            st.session_state.start_clicked = True
            st.rerun()

    st.divider()
    if st.button("🧹 清理殘檔", use_container_width=True):
        cnt = pm.cleanup_webm()
        st.toast(f"已清理 {cnt} 個殘留檔案")
    st.caption(f"字幕位置:\\n`{SUBTITLE_DIR}`")
    size_cmd = f"du -sh {SUBTITLE_DIR} 2>/dev/null | cut -f1"
    total_size = subprocess.run(size_cmd, shell=True, capture_output=True, text=True).stdout.strip()
    st.caption(f"目前大小: {total_size}")

# ── Live multi-worker status ──
st.subheader("📡 即時下載狀態")

status = PodcastManager.read_live_status()
phase = status.get("phase", "idle")

if pm.is_downloading and phase == "idle":
    phase = "init"

if phase == "init":
    st.info("🔄 準備中...")
    time.sleep(1)
    st.rerun()

elif phase == "downloading":
    workers = status.get("workers", [])
    active = status.get("active_count", 0)
    done_c = status.get("done_count", 0)
    fail_c = status.get("fail_count", 0)
    no_subs_c = status.get("no_subs_count", 0)
    total_st = status.get("total", 1)
    total_done = done_c + fail_c + no_subs_c
    if total_st <= 0:
        total_st = 1
    prog_val = min(max(total_done / total_st, 0.0), 1.0)
    st.caption(
        f"⚡ {active} 個線程活躍中 "
        f"| ✅ {done_c} 完成 "
        f"| 🚫 {no_subs_c} 無字幕 "
        f"| ❌ {fail_c} 失敗"
    )
    st.progress(prog_val, text=f"{total_done}/{total_st} 集")

    for w in workers:
        t = w.get("title", "?")
        pct = w.get("dl_pct", 0.0)
        wp = w.get("phase", "?")
        label = t[:55] + "..." if len(t) > 55 else t
        if wp == "download":
            st.progress(pct / 100, text=f"⬇️ {label} — {pct:.1f}%")
        elif wp == "subs_written":
            st.success(f"📝 字幕已寫入: {label}")
        elif wp == "retry_auto":
            st.info(f"🔄 嘗試自動字幕: {label}")
        elif wp == "no_subs":
            st.warning(f"🚫 無字幕: {label}")
        elif wp == "done":
            st.success(f"✅ {label}")
        elif wp == "failed":
            st.error(f"❌ {label}")

    time.sleep(2)
    st.rerun()

elif phase == "complete":
    s = pm.get_summary()
    if s["remaining"] == 0:
        st.success("🎉 **全部字幕下載完成！**")
    else:
        st.warning(f"⚠️ 下載完成，但仍有 {s['remaining']} 集待處理")
    pm._refresh_state_from_disk()

elif phase == "stopped":
    st.warning("⏸️ 已停止下載")
    if pm.get_summary()["remaining"] > 0:
        st.caption(f"剩餘 {pm.get_summary()['remaining']} 集未下載")

elif phase == "idle":
    s = pm.get_summary()
    if s["remaining"] == 0:
        st.success("🎉 全部集數字幕已下載完成！")
    else:
        st.info(f"🟢 待命 — 還有 {s['remaining']} 集字幕待下載")

# ── Episode table (always shown) ──
st.divider()
st.subheader("📋 集數列表")
st.caption("下載 YouTube 自動字幕（.vtt），早期無字幕集數會標記為 🚫 無字幕")

status_filter = st.radio(
    "篩選狀態",
    ["全部", "待下載", "已下載", "無字幕", "略過"],
    horizontal=True,
    key="ep_filter_radio"
)

eps = pm.get_episodes_table()
if status_filter == "待下載":
    filtered = [e for e in eps if e["status"] == "⏳ 待下載"]
elif status_filter == "已下載":
    filtered = [e for e in eps if e["status"] == "✅ 已下載"]
elif status_filter == "無字幕":
    filtered = [e for e in eps if e["status"] == "🚫 無字幕"]
elif status_filter == "略過":
    filtered = [e for e in eps if e["status"] == "⏭️ 略過"]
else:
    filtered = eps

data = [
    [e["status"], e["title"][:60] + ("..." if len(e["title"]) > 60 else ""), e["duration"], e["size"]]
    for e in filtered
]

st.dataframe(
    data,
    column_config={
        0: st.column_config.TextColumn("狀態", width=80),
        1: st.column_config.TextColumn("標題", width=400),
        2: st.column_config.TextColumn("長度", width=70),
        3: st.column_config.TextColumn("大小", width=60),
    },
    height=600,
    hide_index=True,
)

st.caption(
    f"顯示 {len(filtered)} 集 "
    f"| 字幕目錄: {SUBTITLE_DIR} "
    f"| 每 2 秒自動更新 "
    f"| 線程數: {st.session_state.worker_count}"
)