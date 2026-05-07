# 科技浪 Podcast 字幕下載管理器

儲存位置: `/home/thumb/hermes-memory-skills/project-notes/tech_wav_eps/subtitles/`
總集數: 136 集 EP（不含 XEP）
音檔: 已全部下載完成（約 66 集 ~2.7GB，已停止下載音檔）
字幕: 待批量下載（yt-dlp --write-subs --skip-download --sub-format vtt）

## 啟動
```bash
cd /home/thumb/tech_wav_project
streamlit run ui/app.py --server.port 8502 --server.headless true
```

## 下載策略
1. 優先下載手動字幕 `--write-subs --sub-langs all`
2. 無手動字幕則嘗試自動字幕 `--write-auto-subs`
3. 完全無字幕則標記為 🚫 無字幕，不會重試
4. 輸出格式 `.vtt`（WebVTT）

## Cron 自動下載
可在 Hermes 用 cron 定時執行：
```python
from modules.podcast_manager import PodcastManager
pm = PodcastManager()
if pm.get_summary()['remaining'] > 0:
    pm.MAX_WORKERS = 3
    pm.download_all_parallel()
```

## 狀態檔案
- `tech_wav_episodes.json` — 集數清單
- `tech_wav_subtitle_state.json` — 字幕下載狀態
- `tech_wav_eps/subtitles/` — 字幕檔案