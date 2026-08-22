# -*- coding: utf-8 -*-
"""
Servidor simulado de la API de OpenRouter para probar main.py sin gastar créditos.

Implementa la forma oficial de la API (https://openrouter.ai/docs):
  - POST /api/v1/chat/completions        -> storyboard JSON de 6 escenas (prompt +
                                            narración); si pide "Reescribe el prompt" o
                                            "Reescribe la narración" devuelve una versión apta
  - POST /api/v1/videos                  -> 202 {id, status: pending, polling_url}; los prompts
                                            con "corridors" fallan con error de política hasta
                                            que se reescriben (restricción legal simulada)
  - GET  /api/v1/videos/{id}             -> "in_progress" y luego "completed" con unsigned_urls
  - GET  /api/v1/videos/{id}/content     -> sirve el MP4 (ruta de descarga alternativa)
  - POST /api/v1/audio/speech            -> TTS: devuelve un MP3 de tono; los textos con
                                            "marca" fallan por política (restricción de audio)
  - GET  /api/v1/videos/models           -> metadatos reales de los modelos de vídeo
  - GET  /api/v1/models                  -> catálogo mínimo de LLM

Uso:
    python test_mock_server.py             # flujo normal (6 clips + narración + restricciones)
    python test_mock_server.py --no-rephrase  # restricciones sin reescritura -> clip de reserva
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HOST, PORT = "127.0.0.1", 8787
ROOT = Path(__file__).resolve().parent
CLIPS_DIR = ROOT / "mock_clips"
CLIP_DURATION = 3

STORYBOARD_JSON = json.dumps({
    "scenes": [
        {"title": "Inicio", "prompt": "A cat astronaut floats into a space station, camera pans slowly.",
         "narration": "Así comienza la aventura espacial.", "duration": 5},
        {"title": "Pasillo", "prompt": "The cat astronaut walks through neon corridors, tracking shot.",
         "narration": "El robot mira la marca de la nave.", "duration": 4},
        {"title": "Ventana", "prompt": "The cat astronaut looks out the window at Earth, slow push-in.",
         "narration": "La Tierra brilla a lo lejos.", "duration": 6},
        {"title": "Avería", "prompt": "The cat astronaut repairs a broken panel, close-up.",
         "narration": "Todo parece perdido.", "duration": 5},
        {"title": "Éxito", "prompt": "The cat astronaut celebrates, wide shot.",
         "narration": "La misión está salvada.", "duration": 7},
        {"title": "Final", "prompt": "The cat astronaut flies home, epic finale.",
         "narration": "Y así termina la historia.", "duration": 6},
    ]
}, ensure_ascii=False)

SAFE_VIDEO_PROMPT = ("A family-friendly animated cat astronaut exploring a futuristic "
                     "space station, soft lighting, gentle camera movement, "
                     "original characters, no copyrighted material.")
SAFE_NARRATION = "La nave brilla en la oscuridad."

VIDEO_MODELS = [
    {"id": "bytedance/seedance-2.0-mini", "supported_durations": list(range(4, 16)),
     "supported_resolutions": ["480p", "720p"],
     "supported_aspect_ratios": ["1:1", "3:4", "9:16", "4:3", "16:9", "21:9", "9:21"],
     "generate_audio": True},
    {"id": "google/veo-3.1", "supported_durations": [4, 6, 8],
     "supported_resolutions": ["720p", "1080p", "4K"],
     "supported_aspect_ratios": ["16:9", "9:16"], "generate_audio": True},
    {"id": "google/veo-3.1-fast", "supported_durations": [4, 6, 8],
     "supported_resolutions": ["720p", "1080p", "4K"],
     "supported_aspect_ratios": ["16:9", "9:16"], "generate_audio": True},
    {"id": "google/veo-3.1-lite", "supported_durations": [4, 6, 8],
     "supported_resolutions": ["720p", "1080p"],
     "supported_aspect_ratios": ["16:9", "9:16"], "generate_audio": True},
]

_job_state: dict[str, dict] = {}
_job_counter = 0
_lock = threading.Lock()


def make_demo_clips() -> list[Path]:
    CLIPS_DIR.mkdir(parents=True, exist_ok=True)
    sources = [
        "testsrc2=size=640x360:rate=24",
        "smptebars=size=640x360:rate=24",
        "rgbtestsrc=size=640x360:rate=24",
    ]
    freqs = [330, 440, 550]
    clips = []
    for i, (src, freq) in enumerate(zip(sources, freqs), start=1):
        p = CLIPS_DIR / f"clip_{i}.mp4"
        if not p.exists():
            subprocess.run(
                ["ffmpeg", "-y", "-f", "lavfi", "-i", src,
                 "-f", "lavfi", "-i", f"sine=frequency={freq}:duration={CLIP_DURATION}",
                 "-t", str(CLIP_DURATION), "-c:v", "libx264", "-pix_fmt", "yuv420p",
                 "-c:a", "aac", "-shortest", str(p)],
                capture_output=True, check=True,
            )
        clips.append(p)
    return clips


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):  # silencioso
        pass

    def _send_json(self, obj: dict, code: int = 200):
        body = json.dumps(obj).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_body(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            return {}
        return json.loads(self.rfile.read(length))

    def _send_bytes(self, data: bytes, ctype: str, code: int = 200):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_POST(self):
        global _job_counter
        body = self._read_body()
        if self.path == "/api/v1/chat/completions":
            content = ""
            try:
                content = body["messages"][-1]["content"]
            except (KeyError, IndexError, TypeError):
                pass
            if "Reescribe el prompt" in content:
                if os.environ.get("MOCK_NO_REPHRASE") == "1":
                    self._send_json({"choices": [{"message": {"role": "assistant", "content": ""}}]})
                else:
                    self._send_json({"choices": [{"message": {"role": "assistant", "content": SAFE_VIDEO_PROMPT}}]})
            elif "Reescribe la narración" in content:
                if os.environ.get("MOCK_NO_REPHRASE") == "1":
                    self._send_json({"choices": [{"message": {"role": "assistant", "content": ""}}]})
                else:
                    self._send_json({"choices": [{"message": {"role": "assistant", "content": SAFE_NARRATION}}]})
            else:
                self._send_json({"choices": [{"message": {"role": "assistant", "content": STORYBOARD_JSON}}]})
        elif self.path == "/api/v1/videos":
            prompt = str(body.get("prompt", ""))
            restricted = ("corridors" in prompt and "family-friendly" not in prompt)
            with _lock:
                _job_counter += 1
                n = _job_counter
                job_id = f"job-{n}"
                _job_state[job_id] = {
                    "polls": 0,
                    "clip": f"clip_{((n - 1) % 3) + 1}.mp4",
                    "fail": restricted,
                }
            self._send_json({
                "id": job_id,
                "status": "pending",
                "polling_url": f"/api/v1/videos/{job_id}",
            }, code=202)
        elif self.path == "/api/v1/audio/speech":
            text = str(body.get("input", ""))
            if "marca" in text:
                self._send_json(
                    {"error": {"message": "Content policy violation: trademark detected. "
                                          "Modify the text and try again."}},
                    code=400,
                )
            else:
                # duración variable según el texto, para simular narraciones distintas
                dur = int(max(2, min(8, len(text) / 8)))
                tmp = CLIPS_DIR / "tts_tmp.mp3"
                subprocess.run(
                    ["ffmpeg", "-y", "-f", "lavfi", "-i", f"sine=frequency=660:duration={dur}",
                     "-t", str(dur), "-c:a", "libmp3lame", "-b:a", "96k", str(tmp)],
                    capture_output=True, check=True,
                )
                self._send_bytes(tmp.read_bytes(), "audio/mpeg")
        else:
            self._send_json({"error": {"code": 404, "message": f"Ruta no simulada: {self.path}"}}, 404)

    def do_GET(self):
        if self.path.startswith("/api/v1/videos/models"):
            self._send_json({"data": VIDEO_MODELS})
        elif self.path.startswith("/api/v1/videos/") and "/content" in self.path:
            job_id = self.path.split("/api/v1/videos/")[1].split("/")[0]
            state = _job_state.get(job_id)
            if not state:
                self._send_json({"error": {"code": 404, "message": "trabajo no encontrado"}}, 404)
                return
            self._serve_clip(state["clip"])
        elif self.path.startswith("/api/v1/videos/"):
            job_id = self.path.rsplit("/", 1)[-1]
            state = _job_state.get(job_id)
            if not state:
                self._send_json({"error": {"code": 404, "message": "trabajo no encontrado"}}, 404)
                return
            state["polls"] += 1
            if state.get("fail") and state["polls"] >= 2:
                self._send_json({
                    "id": job_id,
                    "status": "failed",
                    "error": "Content policy violation: copyrighted material detected. "
                             "Modify the prompt and try again.",
                })
            elif state["polls"] < 2:
                self._send_json({"id": job_id, "status": "in_progress"})
            else:
                url = f"http://{HOST}:{PORT}/clips/{state['clip']}"
                self._send_json({
                    "id": job_id,
                    "status": "completed",
                    "generation_id": f"gen-{job_id}",
                    "unsigned_urls": [url],
                })
        elif self.path.startswith("/clips/"):
            self._serve_clip(self.path.rsplit("/", 1)[-1])
        elif self.path == "/api/v1/models":
            self._send_json({"data": [
                {"id": "google/gemini-2.5-flash", "name": "Google Gemini 2.5 Flash"},
                {"id": "openai/gpt-4o-mini", "name": "OpenAI GPT-4o mini"},
            ]})
        else:
            self._send_json({"error": {"code": 404, "message": f"Ruta no simulada: {self.path}"}}, 404)

    def _serve_clip(self, name: str):
        file = CLIPS_DIR / name
        if not file.exists():
            self._send_json({"error": {"code": 404, "message": "clip no existe"}}, 404)
            return
        data = file.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", "video/mp4")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


def run_pipeline() -> int:
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    print(f"✔ Mock de OpenRouter en http://{HOST}:{PORT}")

    env = dict(os.environ)
    env["OPENROUTER_API_KEY"] = "sk-test-fake"
    env["OPENROUTER_BASE_URL"] = f"http://{HOST}:{PORT}/api/v1"

    cmd = [sys.executable, str(ROOT / "main.py"),
           "--idea", "Un gato astronauta explora una estación espacial",
           "--model", "bytedance/seedance-2.0-mini",
           "--clips", "6",
           "--duration", "5",
           "--resolution", "1080p",   # no soportado por seedance-mini -> aviso y 'auto'
           "--poll-interval", "1",
           "--timeout", "120",
           "--out-dir", str(ROOT / "out_mock")]
    print("▶ Ejecutando main.py contra el mock…")
    r = subprocess.run(cmd, env=env, capture_output=True, text=True, encoding="utf-8", errors="replace")
    print(r.stdout)
    if r.stderr.strip():
        print("STDERR:", r.stderr[-1500:])
    server.shutdown()

    final = ROOT / "out_mock" / "final_video.mp4"
    if r.returncode != 0:
        print("❌ main.py terminó con error:", r.returncode)
        return 1
    if not final.exists():
        print("❌ No se generó final_video.mp4")
        return 1
    print(f"✔ final_video.mp4 generado ({final.stat().st_size / 1e6:.1f} MB)")
    return 0


if __name__ == "__main__":
    if "--no-rephrase" in sys.argv:
        os.environ["MOCK_NO_REPHRASE"] = "1"  # fuerza el clip de reserva
    make_demo_clips()
    sys.exit(run_pipeline())
