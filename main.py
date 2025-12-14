import os
import shlex
import subprocess
import threading
import time
from pathlib import Path
from typing import Dict, List

import requests


CONFIG_FILE = Path(__file__).with_name("config.txt")
LOG_FILE = Path(__file__).with_name("log_error.txt")
REPORT_INTERVAL_SEC = 3.0
FETCH_INTERVAL_SEC = 1.0
PRINT_INTERVAL_SEC = 1.0
STATUS_INTERVAL_SEC = 3.0
CLEAR_INTERVAL_SEC = 120.0


def load_room_hash() -> str:
    if CONFIG_FILE.exists():
        saved = CONFIG_FILE.read_text(encoding="utf-8").strip()
        if saved:
            return saved

    room_hash = input("Enter room hash: ").strip()
    while not room_hash:
        room_hash = input("Room hash cannot be empty. Enter room hash: ").strip()

    CONFIG_FILE.write_text(room_hash, encoding="utf-8")
    return room_hash


def append_error_log(serial: str, message: str) -> None:
    timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
    try:
        with LOG_FILE.open("a", encoding="utf-8") as f:
            f.write(f"{timestamp}   {serial}   :   {message}\n")
    except Exception:
        # keep silent on logging failures
        pass


def run_adb_once(serial: str, command_text: str) -> Dict[str, object]:
    cmd = ["adb", "-s", serial] + shlex.split(command_text)
    code = -1
    out = ""
    err = ""
    try:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        out, err = proc.communicate()
        code = proc.returncode
    except Exception as exc:
        err = str(exc)
    return {
        "serial": serial,
        "code": code,
        "stdout": (out or "").strip(),
        "stderr": (err or "").strip(),
    }


def start_reporter(room_hash_value: str, stop_signal: threading.Event, interval: float = REPORT_INTERVAL_SEC) -> None:
    """
    Background thread that reports devices every `interval` seconds.
    """
    url = "http://160.25.81.154:9000/api/v1/report-devices"
    payload = {
        "room_hash": room_hash_value,
        "devices": [
            {
                "serial": "abc",
                "data": {},
                "status": "active",
                "device_type": "android",
            }
        ],
    }

    def report_loop() -> None:
        while not stop_signal.is_set():
            try:
                requests.post(url, json=payload, timeout=5)
            except Exception as exc:
                print(f"[report err] {exc}")
            stop_signal.wait(interval)

    threading.Thread(target=report_loop, daemon=True).start()


def start_command_fetcher(
    room_hash_value: str,
    commands: List[Dict[str, str]],
    commands_lock: threading.Lock,
    stop_signal: threading.Event,
    interval: float = FETCH_INTERVAL_SEC,
) -> None:
    """
    Background thread to poll subscribe API and store commands (command_text, serial) in a shared list.
    """
    url = f"http://160.25.81.154:9000/api/v1/subscribe/{room_hash_value}"

    def fetch_loop() -> None:
        while not stop_signal.is_set():
            try:
                resp = requests.get(url, timeout=5)
                if resp.status_code == 200:
                    data = resp.json()
                    cmd_items = data.get("commands") or []
                    simplified = [
                        {
                            "command_text": item.get("command_text", ""),
                            "serial": item.get("serial", ""),
                        }
                        for item in cmd_items
                        if item.get("command_text") and item.get("serial")
                    ]
                    if simplified:
                        with commands_lock:
                            if commands:
                                # still pending; skip adding new commands until queue is empty
                                pass
                            else:
                                commands.extend(simplified)
                else:
                    print(f"[fetch warn] HTTP {resp.status_code}")
            except Exception as exc:
                print(f"[fetch err] {exc}")
            stop_signal.wait(interval)

    threading.Thread(target=fetch_loop, daemon=True).start()


def start_command_printer(
    commands: List[Dict[str, str]],
    commands_lock: threading.Lock,
    stop_signal: threading.Event,
    game_sessions: Dict[str, Dict[str, object]],
    game_sessions_lock: threading.Lock,
    interval: float = PRINT_INTERVAL_SEC,
) -> None:
    """
    Background thread to consume queued commands.
    - Start game commands run persistently per-serial (auto-restart on crash).
    - Stop game commands stop any running game process and execute the stop command once.
    - Other commands run once with summary + error logging.
    """

    def handle_start_game(serial: str, command_text: str) -> None:
        with game_sessions_lock:
            session = game_sessions.get(serial)
            if session and session.get("thread") and session["thread"].is_alive():
                return
            stop_evt = threading.Event()
            session = {"stop": stop_evt, "thread": None, "process": None}
            game_sessions[serial] = session

        cmd = ["adb", "-s", serial] + shlex.split(command_text)

        def loop() -> None:
            while not stop_evt.is_set():
                proc = None
                try:
                    proc = subprocess.Popen(
                        cmd,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE,
                        text=True,
                    )
                    with game_sessions_lock:
                        session["process"] = proc
                    out, err = proc.communicate()
                    code = proc.returncode
                except Exception as exc:
                    _ = exc  # ignore logging for start commands
                finally:
                    with game_sessions_lock:
                        session["process"] = None
                if stop_evt.is_set():
                    break
                stop_evt.wait(1)

        thread = threading.Thread(target=loop, daemon=True)
        session["thread"] = thread
        thread.start()

    def handle_stop_game(serial: str, command_text: str) -> None:
        with game_sessions_lock:
            session = game_sessions.get(serial)
        if session:
            stop_evt = session.get("stop")
            if stop_evt:
                stop_evt.set()
            proc = session.get("process")
            if proc and proc.poll() is None:
                try:
                    proc.terminate()
                    proc.wait(timeout=5)
                except Exception:
                    try:
                        proc.kill()
                    except Exception:
                        pass
            thread = session.get("thread")
            if thread:
                thread.join(timeout=5)
            with game_sessions_lock:
                game_sessions.pop(serial, None)

        result = run_adb_once(serial, command_text)

    def run_regular_command(
        serial: str,
        command_text: str,
        results: List[Dict[str, str]],
        results_lock: threading.Lock,
    ) -> None:
        result = run_adb_once(serial, command_text)
        with results_lock:
            results.append(result)

    def print_loop() -> None:
        while not stop_signal.is_set():
            batch: List[Dict[str, str]] = []
            with commands_lock:
                if commands:
                    batch = commands[:]

            if not batch:
                stop_signal.wait(interval)
                continue

            start_batch: List[Dict[str, str]] = []
            stop_batch: List[Dict[str, str]] = []
            regular_batch: List[Dict[str, str]] = []

            for cmd in batch:
                serial = cmd.get("serial", "")
                text = cmd.get("command_text", "")
                if not serial or not text:
                    continue
                if "nat.myc.test/androidx.test.runner.AndroidJUnitRunner" in text:
                    start_batch.append({"serial": serial, "command_text": text})
                elif "force-stop nat.myc.test" in text:
                    stop_batch.append({"serial": serial, "command_text": text})
                else:
                    regular_batch.append({"serial": serial, "command_text": text})

            for item in start_batch:
                handle_start_game(item["serial"], item["command_text"])

            for item in stop_batch:
                handle_stop_game(item["serial"], item["command_text"])

            if regular_batch:
                workers: List[threading.Thread] = []
                results: List[Dict[str, str]] = []
                results_lock = threading.Lock()
                for item in regular_batch:
                    worker = threading.Thread(
                        target=run_regular_command,
                        args=(item["serial"], item["command_text"], results, results_lock),
                    )
                    workers.append(worker)
                    worker.start()

                for worker in workers:
                    worker.join()

                success_count = sum(1 for r in results if r.get("code") == 0)
                fail_results = [r for r in results if r.get("code") != 0]
                fail_count = len(fail_results)
                timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
                print(f"[SUMARY] {timestamp} : success={success_count} fail={fail_count}")
                for r in fail_results:
                    error_text = r.get("stderr") or r.get("stdout") or f"exit_code={r.get('code')}"
                    append_error_log(r.get("serial", ""), error_text)

            with commands_lock:
                commands.clear()

            stop_signal.wait(interval)

    threading.Thread(target=print_loop, daemon=True).start()


def start_status_monitor(
    stop_signal: threading.Event,
    game_sessions: Dict[str, Dict[str, object]],
    game_sessions_lock: threading.Lock,
    interval: float = STATUS_INTERVAL_SEC,
) -> None:
    """
    Background thread to print counts of alive threads and game processes.
    """

    def monitor_loop() -> None:
        while not stop_signal.is_set():
            thread_count = len(threading.enumerate())
            with game_sessions_lock:
                proc_count = sum(
                    1
                    for sess in game_sessions.values()
                    for proc in [sess.get("process")]
                    if proc and proc.poll() is None
                )
            print(f"[STATUS] threads={thread_count} processes={proc_count}")
            stop_signal.wait(interval)

    threading.Thread(target=monitor_loop, daemon=True).start()


def start_console_clearer(stop_signal: threading.Event, interval: float = CLEAR_INTERVAL_SEC) -> None:
    """
    Background thread to clear console periodically.
    """

    def clear_loop() -> None:
        while not stop_signal.is_set():
            stop_signal.wait(interval)
            if stop_signal.is_set():
                break
            try:
                os.system("cls")
            except Exception:
                pass

    threading.Thread(target=clear_loop, daemon=True).start()


def main() -> None:
    room_hash = load_room_hash()
    print(f"Room hash: {room_hash}")

    commands: List[Dict[str, str]] = []
    commands_lock = threading.Lock()
    stop_event = threading.Event()
    game_sessions: Dict[str, Dict[str, object]] = {}
    game_sessions_lock = threading.Lock()

    start_reporter(room_hash, stop_event)
    start_command_fetcher(room_hash, commands, commands_lock, stop_event)
    start_command_printer(commands, commands_lock, stop_event, game_sessions, game_sessions_lock)
    start_status_monitor(stop_event, game_sessions, game_sessions_lock)
    start_console_clearer(stop_event)
    print("Background threads running. Press Ctrl+C to stop.")

    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("\nStopping...")
        stop_event.set()


if __name__ == "__main__":
    main()
