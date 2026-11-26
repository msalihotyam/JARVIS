"""Record the 30 bench sentences once: one <id>.wav (16 kHz mono 16-bit) per sentence, local only.

Run: python -m dev.stt_bench.record [directory]   (directory must resolve inside talk/stt_bench/audio)
"""

import json
import os
import re
import sys
import tempfile
import wave
from pathlib import Path

PHRASES = Path(__file__).with_name("phrases_tr.json")
AUDIO_ROOT = Path(__file__).resolve().parents[2] / "talk" / "stt_bench" / "audio"
COUNT = 30
RATE, CHANNELS, WIDTH = 16000, 1, 2
MAX_BYTES = 120 * RATE * CHANNELS * WIDTH  # two minutes per sentence
_RESERVED = re.compile(r"^(con|prn|aux|nul|com[0-9¹²³]|lpt[0-9¹²³]|conin\$|conout\$)$", re.IGNORECASE)


def load_sentences(path=PHRASES):
    """Return the 30 reference sentences; raise ValueError unless ids are exactly 01..30."""
    try:
        entries = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, ValueError) as error:
        raise ValueError(f"phrases unreadable: {error}") from None
    if not isinstance(entries, list) or len(entries) != COUNT:
        raise ValueError(f"phrases must be a list of exactly {COUNT} entries")
    sentences = []
    for number, entry in enumerate(entries, 1):
        if not isinstance(entry, dict):
            raise ValueError(f"entry {number}: must be an object")
        reference = entry.get("reference")
        if entry.get("id") != f"{number:02d}":
            raise ValueError(f"entry {number}: id must be {number:02d}")
        if not isinstance(reference, str) or not reference.strip():
            raise ValueError(f"entry {number}: reference must be non-empty text")
        sentences.append(reference)
    return sentences


def allowed_dir(target, root=AUDIO_ROOT):
    """Return the resolved target; raise ValueError unless it is a plain path inside root."""
    raw = os.fspath(target)
    if not isinstance(raw, str) or not raw or "\0" in raw or raw.startswith(("\\\\", "//")):
        raise ValueError("target must be a local path (no UNC or device prefix)")
    path = Path(raw)
    for part in path.parts[1 if path.anchor else 0:]:
        if ":" in part or "~" in part or part != part.rstrip(". ") and part not in (".", ".."):
            raise ValueError(f"target component refused: {part!r}")
        if _RESERVED.match(part.split(".")[0].rstrip(" ")):
            raise ValueError(f"reserved device name: {part!r}")
    base = Path(root).resolve()
    resolved = path.resolve()
    if not resolved.is_relative_to(base):
        raise ValueError("target is outside the allowed audio folder")
    return resolved


class Session:
    """Mouse-driven session state. source.start() begins capture; source.stop() returns raw s16le mono PCM."""

    def __init__(self, sentences, directory, source, root=AUDIO_ROOT, max_bytes=MAX_BYTES):
        if len(sentences) != COUNT:
            raise ValueError(f"need exactly {COUNT} sentences")
        self.sentences = list(sentences)
        self.directory = allowed_dir(directory, root)
        self.source = source
        self.max_bytes = max_bytes
        self.recording = False
        self.finished = False
        self.message = ""
        self.unreadable = {}  # index -> visible "NN.wav okunamadı: <error>" text from the last probe
        todo = [i for i in range(COUNT) if self.status(i) != "recorded"]
        self.index = todo[0] if todo else 0

    def path(self, index):
        return self.directory / f"{index + 1:02d}.wav"

    def _probe(self, index):
        """Return (state, seconds) for one sentence; the only disk read of refresh, it never raises."""
        path = self.path(index)
        try:
            if not path.is_file():
                state, seconds = "missing", 0.0
            else:
                with wave.open(str(path), "rb") as stream:
                    state, seconds = "recorded", stream.getnframes() / stream.getframerate()
        except Exception as error:  # is_file re-raises PermissionError; wave.open raises RuntimeError on a bad chunk
            try:
                detail = f"{type(error).__name__}: {error}"
            except Exception:  # an error whose str() raises still gets a visible message
                detail = type(error).__name__
            self.unreadable[index] = f"{path.name} okunamadı: {detail}"[:200]
            return "unreadable", 0.0
        self.unreadable.pop(index, None)
        return state, seconds

    def status(self, index):
        return self._probe(index)[0]

    def _move(self, step):
        if self.recording:
            self.message = "Önce kaydı bitir."
            return
        self.index = max(0, min(COUNT - 1, self.index + step))
        self.message = ""

    def next(self):
        self._move(1)

    def previous(self):
        self._move(-1)

    def redo(self):
        """Record the current sentence again; the old file stays until the new one is saved."""
        if not self.recording:
            self.toggle()

    def toggle(self):
        if self.finished:
            return
        if not self.recording:
            try:
                self.source.start()
            except Exception as error:  # any source failure leaves the sentence as it was
                self.message = f"Kayıt başlamadı: {error}"
                return
            self.recording, self.message = True, "Kaydediliyor..."
            return
        self.recording = False
        try:
            data = self.source.stop()
        except Exception as error:
            self.message = f"Kayıt alınamadı: {error}"
            return
        self.message = self._save(data)

    def _save(self, data):
        if not isinstance(data, (bytes, bytearray)) or not data:
            return "Ses gelmedi; cümle değişmedi."
        if len(data) > self.max_bytes:
            return "Kayıt çok uzun; cümle değişmedi."
        if len(data) % (CHANNELS * WIDTH):
            return "Bozuk ses verisi; cümle değişmedi."
        temp = None
        try:  # every filesystem step: any failure keeps the old file and leaves no temp
            self.directory.mkdir(parents=True, exist_ok=True)
            handle, temp = tempfile.mkstemp(dir=self.directory, prefix=".rec-", suffix=".tmp")
            os.close(handle)  # reopen by name: no raw descriptor can leak and block the cleanup on Windows
            with open(temp, "wb") as stream, wave.open(stream, "wb") as out:
                out.setnchannels(CHANNELS)
                out.setsampwidth(WIDTH)
                out.setframerate(RATE)
                out.writeframes(bytes(data))
            os.replace(temp, self.path(self.index))
        except Exception as error:
            if temp is not None:
                try:
                    Path(temp).unlink(missing_ok=True)
                except OSError:
                    return f"Kaydedilemedi: {error} (geçici dosya kaldı: {Path(temp).name})"
            return f"Kaydedilemedi: {error}"
        return f"{self.index + 1:02d} kaydedildi."

    def summary(self):
        """Return (recorded count, total seconds) over the readable files on disk; never raises."""
        recorded, seconds = 0, 0.0
        for index in range(COUNT):
            state, length = self._probe(index)
            recorded += state == "recorded"
            seconds += length
        return recorded, seconds

    def finish(self):
        """Salih's "Bitti" click is the only way the session ends."""
        if self.recording:
            self.message = "Önce kaydı bitir."
            return False
        self.finished = True
        return True


def build_window(session):
    """Return the recorder widget for session (QtWidgets only; the audio source is injected)."""
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QHBoxLayout, QLabel, QPushButton, QVBoxLayout, QWidget

    class Window(QWidget):
        def closeEvent(self, event):  # noqa: N802 - Qt override
            if session.finished:
                event.accept()
            else:
                session.message = "Çıkmak için Bitti'ye tıkla."
                refresh()
                event.ignore()

    root = Window()
    root.setWindowTitle("Dikte kaydı")
    sentence = QLabel(objectName="sentence", wordWrap=True, alignment=Qt.AlignmentFlag.AlignCenter)
    font = sentence.font()
    font.setPointSize(28)
    sentence.setFont(font)
    position, progress, message = QLabel(objectName="position"), QLabel(objectName="progress"), QLabel(objectName="message")
    buttons = {}
    row = QHBoxLayout()
    for name, action in (("previous", session.previous), ("toggle", session.toggle), ("redo", session.redo),
                         ("next", session.next), ("done", session.finish)):
        button = QPushButton(objectName=name)
        button.setMinimumSize(120, 56)
        button.clicked.connect(lambda _=False, act=action: click(act))
        row.addWidget(button)
        buttons[name] = button

    def click(act):
        try:
            act()
        except Exception as error:  # the window must never stay stuck on a failed action
            session.message = f"Hata: {error}"
        finally:
            refresh()

    def refresh():
        if session.finished:
            root.close()
            return
        index = session.index
        sentence.setText(session.sentences[index])
        state = {"recorded": "kayıtlı", "missing": "eksik"}.get(session.status(index), "okunamıyor")
        position.setText(f"Cümle {index + 1:02d} / {COUNT} ({state})")
        recorded, seconds = session.summary()
        progress.setText(f"Kaydedilen {recorded}/{COUNT}, toplam {seconds:.1f} sn")
        problems = [session.unreadable[i] for i in sorted(session.unreadable)]
        extra = "\n".join(problems[:3] + ([f"... {len(problems) - 3} dosya daha okunamadı"] if len(problems) > 3 else []))
        message.setText("\n".join(filter(None, (session.message, extra))))
        buttons["toggle"].setText("Bitir" if session.recording else "Kaydet")
        buttons["previous"].setText("Önceki")
        buttons["next"].setText("Sonraki")
        buttons["redo"].setText("Tekrar")
        buttons["done"].setText("Bitti")
        for name in ("previous", "next", "redo", "done"):
            buttons[name].setEnabled(not session.recording)

    layout = QVBoxLayout(root)
    for widget in (position, sentence, message, progress):
        layout.addWidget(widget)
    layout.addLayout(row)
    root.resize(900, 420)
    refresh()
    return root


def window(directory=AUDIO_ROOT):
    """Open the recorder on the default microphone; returns the Qt exit code."""
    from PySide6.QtMultimedia import QAudioFormat, QAudioSource, QMediaDevices
    from PySide6.QtWidgets import QApplication

    class MicSource:
        def __init__(self):
            self.format = QAudioFormat()
            self.format.setSampleRate(RATE)
            self.format.setChannelCount(CHANNELS)
            self.format.setSampleFormat(QAudioFormat.SampleFormat.Int16)
            self.audio = self.device = None
            self.buffer = bytearray()

        def start(self):
            device = QMediaDevices.defaultAudioInput()
            if device.isNull() or not device.isFormatSupported(self.format):
                raise RuntimeError("mikrofon 16 kHz mono 16-bit desteklemiyor")
            self.buffer = bytearray()
            self.audio = QAudioSource(device, self.format)
            self.device = self.audio.start()
            if self.device is None:
                raise RuntimeError("mikrofon açılamadı")
            self.device.readyRead.connect(self._read)

        def _read(self):
            if self.device is not None:
                self.buffer += self.device.readAll().data()

        def stop(self):
            self._read()
            self.audio.stop()
            self.device = None
            return bytes(self.buffer)

    app = QApplication.instance() or QApplication(sys.argv[:1])
    session = Session(load_sentences(), directory, MicSource())
    shown = build_window(session)
    shown.show()
    return app.exec()


if __name__ == "__main__":
    try:
        sys.exit(window(sys.argv[1] if len(sys.argv) > 1 else AUDIO_ROOT))
    except ValueError as error:
        sys.exit(f"Başlatılamadı: {error}")
