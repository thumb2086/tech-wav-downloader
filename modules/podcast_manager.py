import json, os, subprocess, threading, time, re, atexit
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed

PROJECT_DIR = Path("/home/thumb/hermes-memory-skills/project-notes")
EPISODES_JSON = PROJECT_DIR / "tech_wav_episodes.json"
STATE_FILE = PROJECT_DIR / "tech_wav_downloaded_state.json"
AUDIO_DIR = PROJECT_DIR / "tech_wav_eps" / "audio"
STATUS_FILE = Path("/tmp/tech_wav_download_status.json")
MAX_WORKERS = 3
AUDIO_DIR.mkdir(parents=True, exist_ok=True)

PROGRESS_RE = re.compile(r'\[download\]\s+(\d+\.\d+)%\s+of\s+~?([\d.]+)(KiB|MiB|GiB)')

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
            self.state = {"done": [], "skipped": []}
        self._refresh_state_from_disk()

    def _refresh_state_from_disk(self):
        done_set = set(self.state.get("done", []))
        skip_set = set(self.state.get("skipped", []))
        for i, ep in enumerate(self.all_eps):
            t = ep.get("title", "")
            if not t.startswith("EP"):
                continue
            fname = self._safe_fname(i, t)
            if (AUDIO_DIR / fname).exists():
                done_set.add(i)
        self.state["done"] = sorted(done_set)
        self.state["skipped"] = sorted(skip_set)
        self._save_state()

    def _safe_fname(self, idx, title):
        safe = title.replace("/","_").replace(":","_").replace("?","_")
        safe = safe.replace("|","_").replace("！","_").replace("？","_")
        safe = safe.replace("：","_").replace("》","").replace("《","")
        safe = safe.replace('"',"").replace(" ","_")[:100]
        ep_num = title.replace("EP","").split()[0].split("-")[0]
        return f"EP{ep_num}_{safe}.mp3"

    def _save_state(self):
        with open(STATE_FILE, "w") as f:
            json.dump(self.state, f, ensure_ascii=False, indent=2)

    def get_summary(self):
        total = 0
        done = 0
        skipped = 0
        done_set = set(self.state.get("done", []))
        skip_set = set(self.state.get("skipped", []))
        for i, ep in enumerate(self.all_eps):
            if not ep["title"].startswith("EP"):
                continue
            total += 1
            if i in done_set:
                done += 1
            elif i in skip_set:
                skipped += 1
        return {"total": total, "done": done, "remaining": total - done - skipped, "skipped": skipped}

    def get_episodes_table(self):
        result = []
        done_set = set(self.state.get("done", []))
        skip_set = set(self.state.get("skipped", []))
        for i, ep in enumerate(self.all_eps):
            if not ep["title"].startswith("EP"):
                continue
            fname = self._safe_fname(i, ep["title"])
            ap = AUDIO_DIR / fname
            size = ""
            if ap.exists():
                size = f"{ap.stat().st_size / 1024 / 1024:.0f}MB"
            if i in done_set:
                sts = "✅ 已下載"
            elif i in skip_set:
                sts = "⏭️ 略過"
            else:
                sts = "⏳ 待下載"
            result.append({"idx": i, "title": ep["title"], "duration": ep.get("duration_string", "?"), "size": size, "status": sts, "id": ep["id"]})
        return result

    def _download_worker(self, wid, vid, title, idx, fname, ap):
        """Single worker: download one episode, report progress to shared dict."""
        self._workers_status[wid] = {"title": title, "dl_pct": 0.0, "phase": "download", "extract_msg": ""}
        self._emit_workers_status()

        proc = subprocess.Popen(
            ["yt-dlp", "-x", "--audio-format", "mp3", "-o", str(ap),
             "--newline", "--no-colors", f"https://youtube.com/watch?v={vid}"],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
        )

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
            if "[ExtractAudio]" in line:
                self._workers_status[wid]["phase"] = "extract"
                self._workers_status[wid]["extract_msg"] = line.replace("[ExtractAudio]", "").strip()
                self._workers_status[wid]["dl_pct"] = 100.0
                self._emit_workers_status()
                continue
            if "Deleting original file" in line:
                self._workers_status[wid]["phase"] = "extract"
                self._workers_status[wid]["extract_msg"] = "清理原始檔案..."
                self._emit_workers_status()

        proc.wait()
        success = proc.returncode == 0 and ap.exists()
        self._workers_status[wid]["phase"] = "done" if success else "failed"
        self._workers_status[wid]["dl_pct"] = 100.0 if success else self._workers_status[wid].get("dl_pct", 0)
        self._emit_workers_status()
        return success

    def _emit_workers_status(self):
        """Aggregate all workers into the shared status file."""
        active = [w for w in self._workers_status.values() if w["phase"] not in ("done", "failed")]
        done_count = sum(1 for w in self._workers_status.values() if w["phase"] == "done")
        fail_count = sum(1 for w in self._workers_status.values() if w["phase"] == "failed")
        status = {
            "running": True,
            "workers": list(self._workers_status.values()),
            "active_count": len(active),
            "done_count": done_count,
            "fail_count": fail_count,
            "phase": "downloading",
        }
        _write_status(status)

    def download_all_parallel(self):
        """Download remaining episodes with MAX_WORKERS parallel threads."""
        self._stop_flag.clear()
        self._workers_status.clear()

        remaining = [ep for ep in self.get_episodes_table() if ep["status"] == "⏳ 待下載"]
        total = len(remaining)
        if total == 0:
            _write_status({"running": False, "phase": "complete", "workers": [], "active_count": 0, "done_count": 0, "fail_count": 0})
            return

        _write_status({"running": True, "phase": "init", "workers": [], "active_count": 0, "done_count": 0, "fail_count": 0, "total": total})

        completed = 0
        failed = 0
        idx = 0
        pending = list(remaining)

        with ThreadPoolExecutor(max_workers=self.MAX_WORKERS) as executor:
            futures = {}
            # Submit initial batch
            while len(futures) < self.MAX_WORKERS and idx < len(pending):
                ep = pending[idx]
                fname = self._safe_fname(ep["idx"], ep["title"])
                ap = AUDIO_DIR / fname
                wid = f"w{idx}"
                fut = executor.submit(self._download_worker, wid, ep["id"], ep["title"], ep["idx"], fname, ap)
                futures[fut] = (wid, ep)
                idx += 1

            # As each finishes, submit next
            for fut in as_completed(futures):
                wid, ep = futures[fut]
                try:
                    success = fut.result()
                except Exception:
                    success = False

                if success:
                    if ep["idx"] not in self.state["done"]:
                        self.state["done"].append(ep["idx"])
                        self._save_state()
                    completed += 1
                else:
                    if ep["idx"] not in self.state["skipped"]:
                        self.state["skipped"].append(ep["idx"])
                        self._save_state()
                    failed += 1

                # Submit next
                if idx < len(pending):
                    nep = pending[idx]
                    nfname = self._safe_fname(nep["idx"], nep["title"])
                    nap = AUDIO_DIR / nfname
                    nwid = f"w{idx}"
                    nfut = executor.submit(self._download_worker, nwid, nep["id"], nep["title"], nep["idx"], nfname, nap)
                    futures[nfut] = (nwid, nep)
                    idx += 1

                # Remove this future
                del futures[fut]

        _write_status({
            "running": False,
            "phase": "complete",
            "workers": [],
            "active_count": 0,
            "done_count": completed,
            "fail_count": failed,
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
        _write_status({"running": False, "phase": "stopped", "workers": [], "active_count": 0, "done_count": 0, "fail_count": 0})

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
        count = 0
        for f in AUDIO_DIR.glob("*.webm"):
            f.unlink()
            count += 1
        return count