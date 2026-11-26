"""Durable paper-account storage, so P&L history survives app restarts.

Two backends, picked from settings:
  * GistStore  — a private GitHub Gist (PAPER_GIST_ID + GITHUB_TOKEN). Use this on Streamlit
                 Community Cloud, whose disk is wiped whenever the app sleeps, reboots or redeploys.
  * FileStore  — a local JSON file (default; PAPER_STATE_PATH or data/paper_state_<source>.json).

Live and simulator accounts are stored separately so sim trades never pollute live tracking.

Persister runs a small background thread: it loads the saved account once at startup, then
every few seconds snapshots the broker (under the feed lock) and writes it only if it changed.
If the startup load fails it never saves — writing a fresh account would wipe the saved history.
"""
from __future__ import annotations

import json
import logging
import os
import tempfile
import threading
import time

import requests

log = logging.getLogger("btc15m.persist")

GIST_API = "https://api.github.com/gists"


class StoreError(Exception):
    pass


class FileStore:
    kind = "file"

    def __init__(self, path: str):
        self.path = path

    @property
    def label(self) -> str:
        return f"local file {self.path}"

    def load(self) -> dict | None:
        try:
            with open(self.path, "r", encoding="utf-8") as fh:
                return json.load(fh)
        except FileNotFoundError:
            return None
        except (OSError, ValueError) as e:
            raise StoreError(f"Can't read {self.path}: {e}") from e

    def save(self, state: dict) -> None:
        d = os.path.dirname(os.path.abspath(self.path))
        os.makedirs(d, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=d, prefix=".paper_state.", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(state, fh, separators=(",", ":"))
            os.replace(tmp, self.path)
        except OSError as e:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise StoreError(f"Can't write {self.path}: {e}") from e


class GistStore:
    kind = "gist"

    def __init__(self, gist_id: str, token: str, filename: str, session: requests.Session | None = None,
                 timeout: float = 15.0):
        self.gist_id = gist_id.strip()
        self.filename = filename
        self.timeout = timeout
        self.s = session or requests.Session()
        self.s.headers.update({
            "Authorization": f"Bearer {token.strip()}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "paper-terminal",
        })

    @property
    def label(self) -> str:
        return f"GitHub Gist ({self.filename})"

    def load(self) -> dict | None:
        try:
            r = self.s.get(f"{GIST_API}/{self.gist_id}", timeout=self.timeout)
        except requests.RequestException as e:
            raise StoreError(f"Gist load failed: {e}") from e
        if r.status_code != 200:
            raise StoreError(f"Gist load failed: HTTP {r.status_code} {r.text[:120]}")
        f = (r.json().get("files") or {}).get(self.filename)
        if not f:
            return None                     # first run: the file doesn't exist in the gist yet
        content = f.get("content") or ""
        if f.get("truncated"):              # the API inlines at most ~1 MB; fetch the rest raw
            try:
                rr = self.s.get(f["raw_url"], timeout=self.timeout)
                rr.raise_for_status()
                content = rr.text
            except requests.RequestException as e:
                raise StoreError(f"Gist load failed: {e}") from e
        if not content.strip():
            return None
        try:
            return json.loads(content)
        except ValueError as e:
            raise StoreError(f"Gist file {self.filename} is not valid JSON: {e}") from e

    def save(self, state: dict) -> None:
        body = {"files": {self.filename: {"content": json.dumps(state, separators=(",", ":"))}}}
        try:
            r = self.s.patch(f"{GIST_API}/{self.gist_id}", json=body, timeout=self.timeout)
        except requests.RequestException as e:
            raise StoreError(f"Gist save failed: {e}") from e
        if r.status_code != 200:
            raise StoreError(f"Gist save failed: HTTP {r.status_code} {r.text[:120]}")


def make_store(settings) -> FileStore | GistStore | None:
    """Pick the backend from settings. PAPER_PERSIST=off disables persistence."""
    if (settings.persist or "").lower() in ("off", "0", "false", "no"):
        return None
    name = f"paper_state_{'sim' if settings.source == 'sim' else 'live'}.json"
    if settings.gist_id and settings.github_token:
        return GistStore(settings.gist_id, settings.github_token, name)
    return FileStore(settings.state_path or os.path.join("data", name))


class Persister:
    """Loads the account at startup and saves it whenever it changes."""

    def __init__(self, broker, store, lock, interval: float = 5.0):
        self.broker = broker
        self.store = store
        self.lock = lock
        self.interval = interval
        self.loaded = False           # True once the saved state (or "nothing saved yet") was read
        self.restored = False         # True if an earlier account was found and applied
        self.error: str | None = None
        self.last_save_ms: int | None = None
        self._last_json: str | None = None
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()

    # ------------------------------------------------------------------ load / save
    def load(self, attempts: int = 3, delay: float = 2.0) -> bool:
        """Read the saved account into the broker. Call before the feed starts trading."""
        for i in range(attempts):
            try:
                state = self.store.load()
            except StoreError as e:
                self.error = f"{e} — history not loaded, so nothing will be saved this run"
                log.warning("persist load failed (%d/%d): %s", i + 1, attempts, e)
                if i + 1 < attempts:
                    time.sleep(delay)
                continue
            with self.lock:
                if state:
                    try:
                        self.broker.load_state(state)
                    except Exception as e:   # corrupt / incompatible: refuse to overwrite it
                        self.error = (f"Saved account couldn't be read ({type(e).__name__}: {e}) — "
                                      f"not saving, to keep it intact")
                        log.exception("persist restore failed")
                        return False
                    self.restored = True
                self._last_json = json.dumps(self.broker.to_state(), sort_keys=True)
            self.loaded, self.error = True, None
            return True
        return False

    def save_if_changed(self) -> bool:
        if not self.loaded:
            return False
        with self.lock:
            state = self.broker.to_state()
        blob = json.dumps(state, sort_keys=True)
        if blob == self._last_json:
            return False
        try:
            self.store.save(state)
        except StoreError as e:
            self.error = str(e)
            log.warning("persist save failed: %s", e)
            return False
        self._last_json = blob
        self.last_save_ms = int(time.time() * 1000)
        self.error = None
        return True

    # ------------------------------------------------------------------ background loop
    def start(self) -> "Persister":
        if self._thread is None:
            self._thread = threading.Thread(target=self._run, name="btc15m-persist", daemon=True)
            self._thread.start()
        return self

    def _run(self) -> None:
        while not self._stop.wait(self.interval):
            if not self.loaded:
                # Retry the load only while this run hasn't traded: applying old state on top of
                # new activity would double count, so after that we stay read-only.
                with self.lock:
                    fresh = self.broker.is_fresh()
                if fresh:
                    self.load(attempts=1)
                continue
            try:
                self.save_if_changed()
            except Exception as e:      # never let the saver thread die
                self.error = f"{type(e).__name__}: {e}"
                log.exception("persist loop error")

    def status(self) -> dict:
        return {
            "backend": self.store.label,
            "kind": self.store.kind,
            "loaded": self.loaded,
            "restored": self.restored,
            "error": self.error,
            "last_save_ms": self.last_save_ms,
        }
