import json, os, subprocess, threading, time, re, atexit
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed

PROJECT_DIR = Path("/home/thumb/hermes-memory-skills/project-notes")
EPISODES_JSON = PROJECT_DIR / "tech_wav_episodes.json"
STATE_FILE = PROJECT_DIR / "tech_wav_subtitle_state.json"
SUBTITLE_DIR = PROJECT_DIR / "tech_wav_eps" / "subtitles"
STATUS_FILE = Path("/tmp/tech_wav_download_status.json")
MAX_WORKERS = 3
SUBTITLE_DIR.mkdir(parents=True, exist_ok=True)

# yt-dlp progress: [download]  45.2% of ~12.34MiB
PROGRESS_RE = re.compile(r'\[download\]\s+(\d+\.\d+)%\s+of\s+~?([\d.]+)(KiB|MiB|GiB)')
# subtitle written: [info] Writing video subtitles to: ...
SUBS_WRITTEN_RE = re.compile(r'Writing video subtitles to:\s+(.+\.\w+)$')
# no subtitles available warning
NO_SUBS_RE = re.compile(r'\[info\]\s+\d+:\s+doesn\'t have subtitles')

_status_lock = threading.Lock()

def _write_status(data):
    with _status_lock:
        with open(STATUS_FILE, "w") as f:
            json.dump(data, f, ensure_ascii=False)

def _clear_status():
    if STATUS_FILE.exists():
        STATUS_FILE.unlink()

atexit.register(_clear_status)


class PodcastManager:
    MAX_WORKERS = 3

    def __init__(self):
        self._download_thread = None
        self._stop_flag = threading.Event()
        self._workers_status = {}  # {worker_id: {...}}
        self._load_data()

    def _load_data(self):
        with open(EPISODES_JSON) as f:
            self.all_eps = json.load(f)
        if STATE_FILE.exists():
            with open(STATE_FILE) as f:
                self.state = json.load(f)
        else:
            self.state = {"done": [], "skipped": [], "no_subs": []}
        self._refresh_state_from_disk()

    def _refresh_state_from_disk(self):
        done_set = set(self.state.get("done", []))
        skip_set = set(self.state.get("skipped", []))
        no_subs_set = set(self.state.get("no_subs", []))
        for i, ep in enumerate(self.all_eps):
            t = ep.get("title", "")
            if not t.startswith("EP"):
                continue
            # Check if subtitle file exists on disk
            fname = self._safe_fname(i, t, ext=".vtt")
            if (SUBTITLE_DIR / fname).exists():
                done_set.add(i)
            elif i not in no_subs_set:
                # check .srt as fallback
                fname_srt = self._safe_fname(i, t, ext=".srt")
                if (SUBTITLE_DIR / fname_srt).exists():
                    done_set.add(i)
        self.state["done"] = sorted(done_set)
        self.state["skipped"] = sorted(skip_set)
        self.state["no_subs"] = sorted(no_subs_set)
        self._save_state()

    def _safe_fname(self, idx, title, ext=".vtt"):
        safe = title.replace("/", "_").replace(":", "_").replace("?", "_")
        safe = safe.replace("|", "_").replace("！", "_").replace("？", "_")
        safe = safe.replace("：", "_").replace("》", "").replace("《", "")
        safe = safe.replace('"', "").replace(" ", "_")[:100]
        ep_num = title.replace("EP", "").split()[0].split("-")[0]
        return f"EP{ep_num}_{safe}{ext}"

    def _save_state(self):
        with open(STATE_FILE, "w") as f:
            json.dump(self.state, f, ensure_ascii=False, indent=2)

    def get_summary(self):
        total = 0
        done = 0
        skipped = 0
        no_subs = 0
        done_set = set(self.state.get("done", []))
        skip_set = set(self.state.get("skipped", []))
        no_subs_set = set(self.state.get("no_subs", []))
        for i, ep in enumerate(self.all_eps):
            if not ep["title"].startswith("EP"):
                continue
            total += 1
            if i in done_set:
                done += 1
            elif i in skip_set:
                skipped += 1
            elif i in no_subs_set:
                no_subs += 1
        return {
            "total": total,
            "done": done,
            "remaining": total - done - skipped - no_subs,
            "skipped": skipped,
            "no_subs": no_subs,
        }

    def get_episodes_table(self):
        result = []
        done_set = set(self.state.get("done", []))
        skip_set = set(self.state.get("skipped", []))
        no_subs_set = set(self.state.get("no_subs", []))
        for i, ep in enumerate(self.all_eps):
            if not ep["title"].startswith("EP"):
                continue
            fname = self._safe_fname(i, ep["title"], ext=".vtt")
            sub_path = SUBTITLE_DIR / fname
            fname_srt = self._safe_fname(i, ep["title"], ext=".srt")
            sub_path_srt = SUBTITLE_DIR / fname_srt

            size = ""
            if sub_path.exists():
                size = f"{sub_path.stat().st_size / 1024:.0f}KB"
            elif sub_path_srt.exists():
                size = f"{sub_path_srt.stat().st_size / 1024:.0f}KB"

            if i in done_set:
                sts = "✅ 已下載"
            elif i in no_subs_set:
                sts = "🚫 無字幕"
            elif i in skip_set:
                sts = "⏭️ 略過"
            else:
                sts = "⏳ 待下載"

            result.append({
                "idx": i,
                "title": ep["title"],
                "duration": ep.get("duration_string", "?"),
                "size": size,
                "status": sts,
                "id": ep["id"],
            })
        return result

    def _download_worker(self, wid, vid, title, idx, fname, ap):
        """Single worker: download subtitles for one episode."""
        self._workers_status[wid] = {
            "title": title,
            "dl_pct": 0.0,
            "phase": "download",
            "extract_msg": "",
            "has_subs": False,
        }
        self._emit_workers_status()

        # yt-dlp: try to download subtitles only
        proc = subprocess.Popen(
            [
                "yt-dlp",
                "--write-subs",
                "--sub-langs", "all",
                "--sub-format", "vtt",
                "--skip-download",
                "--newline",
                "--no-colors",
                "-o", str(ap.with_suffix("")),   # yt-dlp appends .vtt itself
                f"https://youtube.com/watch?v={vid}",
            ],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
        )

        subtitle_file = None
        no_subs = False

        while True:
            if self._stop_flag.is_set():
                proc.terminate()
                break
            line = proc.stdout.readline()
            if not line:
                break
            line = line.strip()
            m = PROGRESS_RE.search(line)
            if m:
                self._workers_status[wid]["dl_pct"] = float(m.group(1))
                self._workers_status[wid]["phase"] = "download"
                self._emit_workers_status()
                continue
            m = SUBS_WRITTEN_RE.search(line)
            if m:
                subtitle_file = m.group(1)
                self._workers_status[wid]["phase"] = "subs_written"
                self._workers_status[wid]["has_subs"] = True
                self._workers_status[wid]["dl_pct"] = 100.0
                self._emit_workers_status()
                continue
            if NO_SUBS_RE.search(line):
                no_subs = True
                self._workers_status[wid]["phase"] = "no_subs"
                self._workers_status[wid]["dl_pct"] = 100.0
                self._emit_workers_status()
                continue
            if "Writing video subtitles" in line or "has already been recorded" in line:
                continue

        proc.wait()
        success = proc.returncode == 0 and subtitle_file and Path(subtitle_file).exists()

        if not success and not no_subs:
            # Second attempt: try with --write-auto-subs (auto-generated captions)
            self._workers_status[wid]["phase"] = "retry_auto"
            self._workers_status[wid]["dl_pct"] = 0.0
            self._emit_workers_status()

            proc2 = subprocess.Popen(
                [
                    "yt-dlp",
                    "--write-auto-subs",
                    "--sub-langs", "all",
                    "--sub-format", "vtt",
                    "--skip-download",
                    "--newline",
                    "--no-colors",
                    "-o", str(ap.with_suffix("")),
                    f"https://youtube.com/watch?v={vid}",
                ],
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
            )
            while True:
                if self._stop_flag.is_set():
                    proc2.terminate()
                    break
                line = proc2.stdout.readline()
                if not line:
                    break
                line = line.strip()
                m = PROGRESS_RE.search(line)
                if m:
                    self._workers_status[wid]["dl_pct"] = float(m.group(1))
                    self._workers_status[wid]["phase"] = "download"
                    self._emit_workers_status()
                    continue
                m = SUBS_WRITTEN_RE.search(line)
                if m:
                    subtitle_file = m.group(1)
                    self._workers_status[wid]["has_subs"] = True
                    self._workers_status[wid]["phase"] = "subs_written"
                    self._workers_status[wid]["dl_pct"] = 100.0
                    self._emit_workers_status()
                    continue
                if "doesn't have subtitles" in line or NO_SUBS_RE.search(line):
                    no_subs = True
                    self._workers_status[wid]["phase"] = "no_subs"
                    self._workers_status[wid]["dl_pct"] = 100.0
                    self._emit_workers_status()
                    continue

            proc2.wait()
            success = proc2.returncode == 0 and subtitle_file and Path(subtitle_file).exists()

        if no_subs:
            self._workers_status[wid]["phase"] = "no_subs"
        elif success:
            self._workers_status[wid]["phase"] = "done"
        else:
            self._workers_status[wid]["phase"] = "failed"
        self._workers_status[wid]["dl_pct"] = 100.0
        self._emit_workers_status()

        return {"success": success, "no_subs": no_subs, "idx": idx}

    def _emit_workers_status(self):
        active = [w for w in self._workers_status.values() if w["phase"] not in ("done", "failed", "no_subs")]
        done_count = sum(1 for w in self._workers_status.values() if w["phase"] == "done")
        fail_count = sum(1 for w in self._workers_status.values() if w["phase"] == "failed")
        no_subs_count = sum(1 for w in self._workers_status.values() if w["phase"] == "no_subs")
        status = {
            "running": True,
            "workers": list(self._workers_status.values()),
            "active_count": len(active),
            "done_count": done_count,
            "fail_count": fail_count,
            "no_subs_count": no_subs_count,
            "phase": "downloading",
        }
        _write_status(status)

    def download_all_parallel(self):
        self._stop_flag.clear()
        self._workers_status.clear()

        remaining = [ep for ep in self.get_episodes_table() if ep["status"] == "⏳ 待下載"]
        total = len(remaining)
        if total == 0:
            _write_status({
                "running": False, "phase": "complete", "workers": [],
                "active_count": 0, "done_count": 0, "fail_count": 0, "no_subs_count": 0,
            })
            return

        _write_status({
            "running": True, "phase": "init", "workers": [],
            "active_count": 0, "done_count": 0, "fail_count": 0, "no_subs_count": 0, "total": total,
        })

        completed = 0
        failed = 0
        no_subs_count = 0
        idx = 0
        pending = list(remaining)

        with ThreadPoolExecutor(max_workers=self.MAX_WORKERS) as executor:
            futures = {}
            while len(futures) < self.MAX_WORKERS and idx < len(pending):
                ep = pending[idx]
                fname = self._safe_fname(ep["idx"], ep["title"], ext=".vtt")
                ap = SUBTITLE_DIR / fname
                wid = f"w{idx}"
                fut = executor.submit(self._download_worker, wid, ep["id"], ep["title"], ep["idx"], fname, ap)
                futures[fut] = (wid, ep)
                idx += 1

            for fut in as_completed(futures):
                wid, ep = futures[fut]
                try:
                    result = fut.result()
                except Exception:
                    result = {"success": False, "no_subs": False, "idx": ep["idx"]}

                if result["no_subs"]:
                    if ep["idx"] not in self.state["no_subs"]:
                        self.state["no_subs"].append(ep["idx"])
                        self._save_state()
                    no_subs_count += 1
                elif result["success"]:
                    if ep["idx"] not in self.state["done"]:
                        self.state["done"].append(ep["idx"])
                        self._save_state()
                    completed += 1
                else:
                    if ep["idx"] not in self.state["skipped"]:
                        self.state["skipped"].append(ep["idx"])
                        self._save_state()
                    failed += 1

                if idx < len(pending):
                    nep = pending[idx]
                    nfname = self._safe_fname(nep["idx"], nep["title"], ext=".vtt")
                    nap = SUBTITLE_DIR / nfname
                    nwid = f"w{idx}"
                    nfut = executor.submit(self._download_worker, nwid, nep["id"], nep["title"], nep["idx"], nfname, nap)
                    futures[nfut] = (nwid, nep)
                    idx += 1

                del futures[fut]

        _write_status({
            "running": False,
            "phase": "complete",
            "workers": [],
            "active_count": 0,
            "done_count": completed,
            "fail_count": failed,
            "no_subs_count": no_subs_count,
            "total": total,
        })

    def start_download(self):
        if self._download_thread and self._download_thread.is_alive():
            return False
        self._download_thread = threading.Thread(target=self.download_all_parallel, daemon=True)
        self._download_thread.start()
        return True

    def stop_download(self):
        self._stop_flag.set()
        _write_status({
            "running": False, "phase": "stopped", "workers": [],
            "active_count": 0, "done_count": 0, "fail_count": 0, "no_subs_count": 0,
        })

    @property
    def is_downloading(self):
        return self._download_thread is not None and self._download_thread.is_alive()

    @staticmethod
    def read_live_status():
        if not STATUS_FILE.exists():
            return {"running": False, "phase": "idle"}
        try:
            with open(STATUS_FILE) as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            return {"running": False, "phase": "idle"}

    def cleanup_webm(self):
        """No more .webm files expected, but keep for safety."""
        count = 0
        for f in SUBTITLE_DIR.glob("*.webm"):
            f.unlink()
            count += 1
        # Also clean orphan .part / .temp files
        for f in SUBTITLE_DIR.glob("*.part"):
            f.unlink()
            count += 1
        return count