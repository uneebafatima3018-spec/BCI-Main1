"""
Car Racing Game - Flask App
Keyboard + Head Movement control. Shake head 2 times to start.
"""
import os
import threading
import time
from collections import deque

from flask import Flask, Response, render_template, stream_with_context

try:
    import serial  # type: ignore
except Exception:
    serial = None

app = Flask(__name__)

ECG_PORT = os.environ.get("ECG_PORT", "COM14")  # Windows: COM3, Linux: /dev/ttyACM0
ECG_BAUD = int(os.environ.get("ECG_BAUD", "115200"))
ECG_STREAM_HZ = int(os.environ.get("ECG_STREAM_HZ", "100"))  # downsample for browser

_lock = threading.Lock()
_cond = threading.Condition(_lock)
_samples: deque[int] = deque(maxlen=3000)  # ~30s @ 100Hz
_status = "disconnected"
_last_error = ""
_pins_state = "unknown"  # 'unknown' | 'on' | 'off'
_reader_started = False


def _set_status(status: str, err: str = "") -> None:
    global _status, _last_error
    with _cond:
        _status = status
        _last_error = err
        _cond.notify_all()


def _push_sample(v: int) -> None:
    with _cond:
        _samples.append(v)
        _cond.notify_all()


def _set_pins_state(state: str) -> None:
    global _pins_state
    if state not in ("unknown", "on", "off"):
        return
    with _cond:
        _pins_state = state
        _cond.notify_all()


def _serial_reader() -> None:
    if serial is None:
        _set_status("error", "pyserial not installed")
        return

    ser = None
    last_emit = 0.0
    min_interval = 1.0 / max(1, ECG_STREAM_HZ)

    while True:
        try:
            if ser is None or not getattr(ser, "is_open", False):
                try:
                    ser = serial.Serial(ECG_PORT, ECG_BAUD, timeout=1)
                    _set_status("connected", "")
                except Exception as e:
                    _set_status("disconnected", f"Cannot open {ECG_PORT}: {e}")
                    time.sleep(2.0)
                    continue

            line = ser.readline()
            if not line:
                continue
            try:
                s = line.decode("utf-8", errors="ignore").strip()
                if not s:
                    continue
                # Accept:
                #   "123"                 -> sample only
                #   "123,0" (leadOff=0)   -> pins ON
                #   "123,1" (leadOff=1)   -> pins OFF
                parts = s.split(",")
                v = int(parts[0].strip())
                if len(parts) >= 2:
                    lead_off = int(parts[1].strip())
                    _set_pins_state("off" if lead_off else "on")
            except Exception:
                continue

            now = time.time()
            if now - last_emit >= min_interval:
                last_emit = now
                _push_sample(v)
        except Exception as e:
            try:
                if ser:
                    ser.close()
            except Exception:
                pass
            ser = None
            _set_status("disconnected", f"Serial error: {e}")
            time.sleep(1.0)


def _ensure_reader_started() -> None:
    global _reader_started
    if _reader_started:
        return
    _reader_started = True
    threading.Thread(target=_serial_reader, daemon=True).start()


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/ecg-stream")
def ecg_stream():
    """Server-Sent Events stream for ECG samples."""
    _ensure_reader_started()

    def gen():
        last_sent_value = None
        last_status = None
        last_pins = None
        while True:
            with _cond:
                _cond.wait(timeout=1.0)
                status = _status
                err = _last_error
                value = _samples[-1] if _samples else None
                pins = _pins_state

            if status != last_status:
                last_status = status
                yield f"event: status\ndata: {status}\n\n"
                if err:
                    yield f"event: ecg_error\ndata: {err}\n\n"

            if pins != last_pins:
                last_pins = pins
                yield f"event: pins\ndata: {pins}\n\n"

            if value is not None and value != last_sent_value:
                last_sent_value = value
                yield f"event: sample\ndata: {value}\n\n"

    headers = {
        "Cache-Control": "no-cache",
        "X-Accel-Buffering": "no",
        "Connection": "keep-alive",
    }
    return Response(stream_with_context(gen()), mimetype="text/event-stream", headers=headers)


if __name__ == "__main__":
    app.run(debug=True, host="0.0.0.0", port=5000)
