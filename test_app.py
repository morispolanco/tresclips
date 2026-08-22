# -*- coding: utf-8 -*-
"""Prueba el motor en proceso (QueueStream + main.main) y la app Streamlit (AppTest)."""
import os
import queue
import sys
import tempfile
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
# AppTest crea carpetas temporales: usar el workspace (el temp del sistema puede estar bloqueado)
tempfile.tempdir = str(ROOT / "_tmp_streamlit")
os.makedirs(tempfile.tempdir, exist_ok=True)
import app as app_mod  # noqa: E402
import main as m  # noqa: E402


def test_engine_in_process():
    q: "queue.Queue[object]" = queue.Queue()
    argv = ["--demo", "--clips", "3", "--duration", "1", "--out-dir", "out_ui_test"]
    t = threading.Thread(target=app_mod.worker, args=(argv, q, ""), daemon=True)
    t.start()
    lines = []
    exit_code = None
    while True:
        item = q.get(timeout=60)
        if isinstance(item, tuple) and item and item[0] == "__EXIT__":
            exit_code = int(item[1])
            break
        lines.append(str(item))
    assert exit_code == 0, f"exit={exit_code}, cola: {lines[-5:]}"
    assert any("¡Listo!" in ln for ln in lines), lines[-10:]
    assert any("Escena" in ln for ln in lines)
    final = ROOT / "out_ui_test" / "final_video.mp4"
    assert final.exists(), "no se generó final_video.mp4"
    print(f"test_engine_in_process OK (exit={exit_code}, {len(lines)} líneas de log)")


def test_app_renders():
    try:
        from streamlit.testing.v1 import AppTest
        at = AppTest.from_file(str(ROOT / "app.py"), default_timeout=30)
        at.run()
        assert not at.exception, at.exception
        assert at.text_input, "faltan campos de texto"
        assert at.button, "faltan botones"
        print("test_app_renders OK (AppTest sin excepciones)")
    except PermissionError as e:
        print(f"test_app_renders SKIP (el sandbox bloquea chmod en temporales de AppTest): {e}")


if __name__ == "__main__":
    test_engine_in_process()
    test_app_renders()
    print("TODO OK")
