#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
TresClips — 6 clips de 5 s con OpenRouter (Seedance/Veo) + narración en español
              latinoamericano (TTS) + montaje FFmpeg
===============================================================================

Pipeline:
  1. El usuario da UNA idea (texto libre).
  2. OpenRouter (chat completions) convierte la idea en un storyboard de N
     escenas consecutivas (por defecto 6), cada una con un prompt de vídeo y
     una línea de narración en español latinoamericano.
  3. OpenRouter (vídeo, p. ej. bytedance/seedance-2.0-mini) genera los N clips.
  4. OpenRouter (TTS, p. ej. deepgram/aura-2, voz masculina es-LatAm) genera la
     narración de cada escena.
  5. FFmpeg mezcla cada clip con su narración y concatena todo en un MP4 final.

Uso básico:
    python main.py "Un robot explorador descubre una ciudad submarina"
    python main.py --demo          # sin API: prueba el pipeline FFmpeg
    python main.py --list-models   # lista los modelos de vídeo de OpenRouter

Configuración: clave de OpenRouter en la variable OPENROUTER_API_KEY o en .env
(https://openrouter.ai/settings/keys). Requiere saldo/créditos en la cuenta.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path
from urllib.parse import urlparse

import requests

# --------------------------------------------------------------------------
# Constantes
# --------------------------------------------------------------------------
OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"
DEFAULT_VIDEO_MODEL = "bytedance/seedance-2.0-mini"  # vídeo de ByteDance vía OpenRouter (4-15 s)
DEFAULT_TTS_MODEL = "deepgram/aura-2"                # TTS con voces en español latinoamericano
DEFAULT_TTS_VOICE = "aura-2-alvaro-es"               # voz masculina (es-LatAm)
DEFAULT_CLIPS = 6                                    # número de clips
DEFAULT_DURATION = 5                                 # duración base (se ajusta a la narración)
NARRATION_PAD = 0.75                                 # margen (s) entre narración y duración del clip
DEFAULT_LLM_MODEL = "auto"   # "auto" elige el primer modelo disponible (deepseek primero)
LLM_FALLBACKS = [
    "deepseek/deepseek-v4-flash-0731",
    "google/gemini-2.5-flash",
    "openai/gpt-4o-mini",
    "anthropic/claude-haiku-4.5",
]
VEO_MAX_DURATION = 8   # respaldo si no se pueden consultar los metadatos del modelo
POLL_INTERVAL = 10     # segundos entre comprobaciones del estado de generación
CLIP_TIMEOUT = 900     # tiempo máximo de espera por clip (segundos)

BANNER = r"""
  ████████╗██████╗ ███████╗███████╗ ██████╗██╗     ██╗██████╗ ███████╗
  ╚══██╔══╝██╔══██╗██╔════╝██╔════╝██╔════╝██║     ██║██╔══██╗██╔════╝
     ██║   ██████╔╝█████╗  █████╗  ██║     ██║     ██║██████╔╝███████╗
     ██║   ██╔══██╗██╔══╝  ██╔══╝  ██║     ██║     ██║██╔═══╝ ╚════██║
     ██║   ██║  ██║███████╗███████╗╚██████╗███████╗██║██║     ███████║
     ╚═╝   ╚═╝  ╚═╝╚══════╝╚══════╝ ╚═════╝╚══════╝╚═╝╚═╝     ╚══════╝
"""


# --------------------------------------------------------------------------
# Utilidades
# --------------------------------------------------------------------------
class ApiError(Exception):
    """Error de la API de OpenRouter (con código HTTP cuando existe)."""

    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


def load_dotenv(path: str | Path | None = None) -> dict:
    """Carga OPENROUTER_API_KEY y demás variables desde un archivo .env."""
    env: dict = {}
    p = Path(path) if path else Path.cwd() / ".env"
    if p.exists():
        for line in p.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            env[k.strip()] = v.strip().strip('"').strip("'")
    return env


def get_api_key(env: dict) -> str | None:
    """Clave API: primero variable de entorno (p. ej. la introducida en la UI),
    después el archivo .env."""
    return (os.environ.get("OPENROUTER_API_KEY")
            or env.get("OPENROUTER_API_KEY"))


def get_base_url(env: dict) -> str:
    return env.get("OPENROUTER_BASE_URL") or os.environ.get("OPENROUTER_BASE_URL") or OPENROUTER_BASE_URL


def http_json(method: str, url: str, api_key: str, payload: dict | None = None,
              timeout: int = 120) -> dict:
    """Petición JSON a OpenRouter con manejo de errores."""
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    referer = os.environ.get("OPENROUTER_REFERER") or ""
    if referer:
        headers["HTTP-Referer"] = referer
    title = os.environ.get("OPENROUTER_TITLE") or ""
    if title:
        headers["X-Title"] = title
    try:
        r = requests.request(method, url, json=payload, headers=headers, timeout=timeout)
    except requests.RequestException as e:
        raise ApiError(f"No se pudo conectar con {url}: {e}") from e
    if r.status_code >= 400:
        msg = r.text[:400]
        try:
            j = r.json()
            msg = (j.get("error", {}).get("message") if isinstance(j.get("error"), dict) else None) \
                or j.get("message") or msg
        except Exception:
            pass
        raise ApiError(f"HTTP {r.status_code}: {msg}", status=r.status_code)
    if not r.content:
        return {}
    return r.json()


def extract_video_url(data: dict) -> str | None:
    """Busca la URL de descarga del vídeo en los formatos de respuesta conocidos."""
    if not isinstance(data, dict):
        return None
    candidates: list[object] = [
        data.get("video_url"),
        data.get("download_url"),
        data.get("url"),
    ]
    video = data.get("video")
    if isinstance(video, dict):
        candidates += [video.get("url"), video.get("video_url"), video.get("download_url")]
    elif isinstance(video, str):
        candidates.append(video)
    videos = data.get("videos")
    if isinstance(videos, list) and videos and isinstance(videos[0], dict):
        candidates += [videos[0].get("url"), videos[0].get("video_url")]
    for key in ("output", "result", "data", "assets"):
        node = data.get(key)
        if isinstance(node, dict):
            candidates += [node.get("url"), node.get("video_url")]
            media = node.get("video")
            if isinstance(media, dict):
                candidates.append(media.get("url"))
    for c in candidates:
        if isinstance(c, str) and c.startswith(("http://", "https://")):
            return c
    return None


VALID_ASPECTS = ("16:9", "9:16", "1:1", "4:3", "3:2", "3:4", "2:3", "21:9", "9:21")
ASPECT_ALIASES = {
    "h": "16:9", "horizontal": "16:9", "horizontal (16:9)": "16:9", "landscape": "16:9",
    "v": "9:16", "vertical": "9:16", "vertical (9:16)": "9:16", "portrait": "9:16",
    "c": "1:1", "cuadrado": "1:1", "square": "1:1",
}


def normalize_aspect(value: str | None) -> str:
    """Convierte 'horizontal'/'h', 'vertical'/'v', 'cuadrado'/'c' o un ratio a
    la relación de aspecto canónica (16:9, 9:16, 1:1, …)."""
    if not value:
        return "16:9"
    v = str(value).strip().lower()
    if v in ASPECT_ALIASES:
        return ASPECT_ALIASES[v]
    if v in VALID_ASPECTS:
        return v
    print(f"⚠ Proporción '{value}' no reconocida; se usará horizontal (16:9).")
    return "16:9"


def run_cmd(cmd: list[str], cwd: Path | None = None) -> subprocess.CompletedProcess:
    print("     >", " ".join(str(c) for c in cmd))
    return subprocess.run(
        cmd,
        cwd=str(cwd) if cwd else None,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )


def ffprobe_json(ffprobe: str, path: Path) -> dict | None:
    try:
        r = subprocess.run(
            [ffprobe, "-v", "error", "-print_format", "json",
             "-show_streams", "-show_format", str(path)],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
        )
        if r.returncode != 0:
            return None
        return json.loads(r.stdout)
    except Exception:
        return None


def probe_video(ffprobe: str, path: Path) -> dict:
    """Devuelve duración, tamaño, fps y presencia de audio de un vídeo."""
    data = ffprobe_json(ffprobe, path) or {}
    streams = data.get("streams", [])
    vs = next((s for s in streams if s.get("codec_type") == "video"), {})
    has_audio = any(s.get("codec_type") == "audio" for s in streams)

    duration = None
    try:
        duration = float(data.get("format", {}).get("duration") or vs.get("duration"))
    except (TypeError, ValueError):
        duration = None

    fps = None
    rate = vs.get("avg_frame_rate") or vs.get("r_frame_rate")
    if rate and "/" in str(rate):
        try:
            n, d = str(rate).split("/")
            fps = round(float(n) / float(d), 3) if float(d) else None
        except (ValueError, ZeroDivisionError):
            fps = None

    return {
        "duration": duration,
        "width": vs.get("width"),
        "height": vs.get("height"),
        "fps": fps,
        "has_audio": has_audio,
    }


# --------------------------------------------------------------------------
# Paso 1: storyboard (idea -> 3 prompts de escena) vía chat de OpenRouter
# --------------------------------------------------------------------------
STORYBOARD_TEMPLATE = """\
Eres un director de vídeo experto y narrador. La idea del usuario para un vídeo es:

"{idea}"

Crea un guion de EXACTAMENTE {num_scenes} escenas consecutivas de {duration} \
segundos cada una (inicio, desarrollo, culminación y desenlace), que juntas \
cuenten la idea completa como una historia visual coherente (mismo \
personaje/sujeto, mismo estilo y continuidad entre escenas).

Para cada escena incluye TRES campos:
1. "prompt": prompt de generación de vídeo detallado EN INGLÉS (los modelos de \
vídeo funcionan mejor en inglés) que describa el sujeto, la acción principal, \
el movimiento de cámara, el estilo visual, la iluminación y el ambiente. Evita \
texto o letras en pantalla.
2. "narration": línea de narración EN ESPAÑOL (latinoamericano), breve (6-10 \
palabras), que siga el hilo de la historia. Sin marcas, personajes famosos ni \
contenido protegido.
3. "duration": duración sugerida en segundos para la escena (número entero de 4 \
a 15), calculada para que la narración quepa holgadamente y el ritmo sea \
natural. Cada escena puede tener una duración distinta.

Responde SOLO con JSON válido con esta estructura exacta:
{{"scenes": [
  {{"title": "...", "prompt": "...", "narration": "...", "duration": 5}},
  ...  (exactamente {num_scenes} elementos)
]}}
"""


def extract_scenes_list(text: str) -> list | None:
    """Extrae la lista de escenas crudas de un texto JSON (dict con 'scenes' o lista)."""
    text = (text or "").strip()
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.S)
    data = None
    try:
        data = json.loads(text)
    except Exception:
        m = re.search(r"\{.*\}", text, re.S)
        if m:
            try:
                data = json.loads(m.group(0))
            except Exception:
                data = None
    scenes = data if isinstance(data, list) else (data.get("scenes") if isinstance(data, dict) else None)
    return scenes if isinstance(scenes, list) else None


def _normalize_scene(s: object) -> dict | None:
    """Convierte un elemento de escena en {prompt, narration, duration} (o None)."""
    if not isinstance(s, dict) or not str(s.get("prompt", "")).strip():
        return None
    try:
        dur = int(s.get("duration"))
    except (TypeError, ValueError):
        dur = None
    return {
        "prompt": str(s["prompt"]).strip(),
        "narration": str(s.get("narration", "")).strip() or None,
        "duration": dur,
    }


def parse_scenes_json(text: str, num_scenes: int = DEFAULT_CLIPS) -> list[dict] | None:
    """Extrae las escenas (prompt + narración + duración) de la respuesta JSON del LLM."""
    scenes = extract_scenes_list(text)
    if not scenes or len(scenes) < num_scenes:
        return None
    out: list[dict] = []
    for s in scenes[:num_scenes]:
        norm = _normalize_scene(s)
        if norm:
            out.append(norm)
    return out if len(out) == num_scenes else None


def parse_script_scenes(text: str) -> list[dict] | None:
    """Parsea un guion JSON del usuario sin exigir un número mínimo de escenas."""
    scenes = extract_scenes_list(text)
    if not scenes:
        return None
    out: list[dict] = []
    for s in scenes:
        norm = _normalize_scene(s)
        if norm:
            out.append(norm)
    return out or None


def chat_completions(base_url: str, api_key: str, model: str, prompt: str,
                     temperature: float = 0.9, json_mode: bool = True) -> str:
    """Una llamada de chat a OpenRouter; devuelve el texto de la respuesta."""
    payload: dict = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": temperature,
    }
    if json_mode:
        try:
            payload["response_format"] = {"type": "json_object"}
            data = http_json("POST", f"{base_url}/chat/completions", api_key, payload)
        except ApiError:
            # algunos modelos no aceptan response_format: reintentar sin él
            payload.pop("response_format", None)
            data = http_json("POST", f"{base_url}/chat/completions", api_key, payload)
    else:
        data = http_json("POST", f"{base_url}/chat/completions", api_key, payload)
    try:
        return str(data["choices"][0]["message"]["content"])
    except (KeyError, IndexError, TypeError) as e:
        raise ApiError(f"Respuesta de chat inesperada: {json.dumps(data)[:300]}") from e


def storyboard_via_openrouter(base_url: str, api_key: str, idea: str,
                              duration: int, llm_model: str,
                              num_scenes: int = DEFAULT_CLIPS) -> list[dict] | None:
    """Pide al LLM de OpenRouter que convierta la idea en N escenas (prompt + narración)."""
    prompt = STORYBOARD_TEMPLATE.format(idea=idea, duration=duration, num_scenes=num_scenes)
    models = [llm_model] if llm_model != "auto" else LLM_FALLBACKS
    for model in models:
        try:
            content = chat_completions(base_url, api_key, model, prompt)
            scenes = parse_scenes_json(content, num_scenes)
            if scenes:
                print(f"     (storyboard generado con {model})")
                return scenes
            print(f"     ! {model} devolvió JSON no válido; pruebo otro modelo…")
        except Exception as e:  # noqa: BLE001
            print(f"     ! {model} falló ({type(e).__name__}: {e}); pruebo otro modelo…")
    return None


SCRIPT_TO_SCENES_TEMPLATE = """\
El usuario quiere convertir en vídeo un guion propio. Guion:

{script}

Convierte el guion en EXACTAMENTE {num_scenes} escenas consecutivas, cada una \
con estos campos:
1. "prompt": prompt de generación de vídeo detallado EN INGLÉS que describa el \
sujeto, la acción principal, el movimiento de cámara, el estilo visual, la \
iluminación y el ambiente. Evita texto o letras en pantalla.
2. "narration": línea de narración EN ESPAÑOL (latinoamericano), breve (6-10 \
palabras), que siga el hilo del guion. Sin marcas, personajes famosos ni \
contenido protegido.
3. "duration": duración sugerida en segundos (entero de 4 a 15) para que la \
narración quepa holgadamente.

Responde SOLO con JSON válido con esta estructura exacta:
{{"scenes": [
  {{"title": "...", "prompt": "...", "narration": "...", "duration": 5}},
  ...  (exactamente {num_scenes} elementos)
]}}
"""


def script_to_scenes(base_url: str, api_key: str, script: str, duration: int,
                     llm_model: str, num_scenes: int = DEFAULT_CLIPS) -> list[dict] | None:
    """Convierte un guion de texto libre en escenas (prompt + narración + duración)."""
    prompt = SCRIPT_TO_SCENES_TEMPLATE.format(script=(script or "")[:4000],
                                              num_scenes=num_scenes)
    models = [llm_model] if llm_model != "auto" else LLM_FALLBACKS
    for model in models:
        try:
            content = chat_completions(base_url, api_key, model, prompt)
            scenes = parse_scenes_json(content, num_scenes)
            if scenes:
                print(f"     (guion convertido con {model})")
                return scenes
            print(f"     ! {model} devolvió JSON no válido; pruebo otro modelo…")
        except Exception as e:  # noqa: BLE001
            print(f"     ! {model} falló ({type(e).__name__}: {e}); pruebo otro modelo…")
    return None


def naive_storyboard(idea: str, duration: int,
                     num_scenes: int = DEFAULT_CLIPS) -> list[dict]:
    """Plan B sin API: plantillas sencillas con narración genérica."""
    tpl = (
        "{parte} de la escena sobre: {idea}. Vídeo cinematográfico, cámara "
        "fluida, iluminación coherente con las demás escenas, sin texto en pantalla."
    )
    parts = [
        ("Inicio", "Así comienza la historia de {idea}."),
        ("Primer desarrollo", "Todo se va complicando."),
        ("Segundo desarrollo", "La aventura sigue su curso."),
        ("Culminación", "Llega el momento más emocionante."),
        ("Desenlace", "El final se acerca."),
        ("Cierre", "Y así termina la historia."),
    ]
    scenes = []
    for parte, nar in parts[:num_scenes]:
        nar_text = nar.format(idea=idea)
        # duración sugerida a partir de la longitud de la narración (4-15 s)
        suggested = max(4, min(15, int(len(nar_text.split()) * 0.5 + 2.5)))
        scenes.append({
            "prompt": tpl.format(parte=parte, idea=idea),
            "narration": nar_text,
            "duration": suggested,
        })
    return scenes


# --------------------------------------------------------------------------
# Paso 2: generación de clips con OpenRouter (vídeo)
# API oficial: POST /api/v1/videos  ->  polling_url  ->  unsigned_urls / content
# --------------------------------------------------------------------------
def url_origin(base_url: str) -> str:
    """Devuelve el origen (esquema + host) de una URL base."""
    p = urlparse(base_url)
    return f"{p.scheme}://{p.netloc}"


def resolve_url(base_url: str, url: str) -> str:
    """Resuelve una polling_url relativa o absoluta contra la API."""
    if url.startswith(("http://", "https://")):
        return url
    if url.startswith("/"):
        return url_origin(base_url) + url
    return f"{base_url.rstrip('/')}/{url.lstrip('/')}"


_MODEL_META_CACHE: dict[str, list] = {}


def fetch_video_models(base_url: str, api_key: str | None = None) -> list:
    """Metadatos de los modelos de vídeo (GET {base}/videos/models, es público).
    Caché por URL base (varias sesiones pueden usar bases distintas)."""
    if base_url not in _MODEL_META_CACHE:
        headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
        try:
            r = requests.get(f"{base_url}/videos/models", headers=headers, timeout=30)
            _MODEL_META_CACHE[base_url] = r.json().get("data", []) if r.status_code == 200 else []
        except Exception:  # noqa: BLE001
            _MODEL_META_CACHE[base_url] = []
    return _MODEL_META_CACHE[base_url]


def model_meta(base_url: str, api_key: str | None, model: str) -> dict:
    return next((m for m in fetch_video_models(base_url, api_key) if m.get("id") == model), {})


def resolve_duration(base_url: str, api_key: str, model: str, requested: int,
                     round_up: bool = False) -> int:
    """Ajusta la duración a lo que soporta el modelo (metadatos oficiales).

    round_up=True elige la duración soportada más pequeña que cubra la pedida
    (para que la narración quepa); por defecto, la mayor que no la supere.
    """
    durs = sorted(int(d) for d in (model_meta(base_url, api_key, model).get("supported_durations") or []))
    if durs:
        if requested in durs:
            return requested
        if round_up:
            ok = [d for d in durs if d >= requested]
            chosen = min(ok) if ok else max(durs)
        else:
            ok = [d for d in durs if d <= requested]
            chosen = max(ok) if ok else min(durs)
        if chosen != requested:
            print(f"⚠ El modelo {model} soporta duraciones {durs} s; se usará {chosen} s."
                  + (f" Añade --lengthen para alargarlo a {requested} s con FFmpeg."
                     if chosen < requested else ""))
        return chosen
    # sin metadatos: respaldo
    if model.lower().startswith("google/veo") and requested > VEO_MAX_DURATION:
        print(f"⚠ {model} genera como máximo {VEO_MAX_DURATION} s por clip; se usará "
              f"{VEO_MAX_DURATION} s" + (f" (--lengthen lo alarga a {requested} s)." if True else ""))
        return VEO_MAX_DURATION
    return requested


def clip_duration_for(base_url: str, api_key: str, model: str,
                      narration_dur: float | None,
                      suggested: int | None, default: int) -> int:
    """Duración del clip: si hay narración, se mide su duración real y se redondea
    hacia arriba a la duración soportada por el modelo (con margen NARRATION_PAD);
    si no, se usa la duración sugerida por el guion o la base por defecto."""
    if narration_dur:
        return resolve_duration(base_url, api_key, model,
                                math.ceil(narration_dur + NARRATION_PAD), round_up=True)
    return resolve_duration(base_url, api_key, model, suggested or default)


def adjust_params(base_url: str, api_key: str, model: str,
                  aspect: str, resolution: str) -> tuple[str, str]:
    """Valida aspect_ratio/resolución contra los metadatos del modelo."""
    meta = model_meta(base_url, api_key, model)
    aspects = meta.get("supported_aspect_ratios") or []
    resos = meta.get("supported_resolutions") or []
    if aspects and aspect not in aspects:
        print(f"⚠ El modelo {model} no soporta {aspect}; se usará 16:9.")
        aspect = "16:9"
    if resolution != "auto" and resos and resolution not in resos:
        print(f"⚠ El modelo {model} no soporta {resolution}; se usará la del proveedor ('auto').")
        resolution = "auto"
    return aspect, resolution


def submit_video(base_url: str, api_key: str, model: str, prompt: str,
                 duration: int, aspect: str, resolution: str,
                 generate_audio: bool) -> dict:
    """Crea el trabajo de vídeo (POST /videos); devuelve {id, polling_url, status}."""
    body: dict = {
        "model": model,
        "prompt": prompt,
        "duration": duration,
        "aspect_ratio": aspect,
    }
    if resolution and resolution != "auto":
        body["resolution"] = resolution
    if generate_audio:
        body["generate_audio"] = True
    # Si la API rechaza algún parámetro opcional (400), se reintenta sin él.
    variants = [
        body,
        {k: v for k, v in body.items() if k != "resolution"},
        {k: v for k, v in body.items() if k not in ("resolution", "aspect_ratio")},
    ]
    last_err: ApiError | None = None
    for variant in variants:
        try:
            data = http_json("POST", f"{base_url}/videos", api_key, variant)
            job_id = data.get("id")
            if not job_id:
                raise ApiError(f"Respuesta sin 'id': {json.dumps(data)[:300]}")
            return {
                "id": str(job_id),
                "polling_url": data.get("polling_url"),
                "status": data.get("status", "pending"),
            }
        except ApiError as e:
            last_err = e
            if e.status != 400:
                raise
    raise last_err or ApiError("No se pudo crear el trabajo de vídeo.")


def poll_video(base_url: str, api_key: str, job: dict,
               timeout: int, poll_interval: int) -> dict:
    """Espera a que el trabajo termine y devuelve la respuesta final."""
    deadline = time.time() + timeout
    data = dict(job)
    while time.time() < deadline:
        status = str(data.get("status", "")).lower()
        if status in ("completed", "succeeded", "success", "complete"):
            return data
        if status in ("failed", "error", "cancelled", "canceled", "expired", "rejected"):
            detalle = data.get("error") or data.get("message") or json.dumps(data)[:300]
            raise ApiError(f"La generación de vídeo terminó en '{status}': {detalle}")
        time.sleep(poll_interval)
        polling_url = data.get("polling_url") or f"/api/v1/videos/{data.get('id')}"
        data = http_json("GET", resolve_url(base_url, str(polling_url)), api_key)
    raise TimeoutError(f"Tiempo de espera agotado tras {timeout}s para el trabajo {data.get('id')}.")


def generate_clip(base_url: str, api_key: str, model: str, prompt: str,
                  duration: int, aspect: str, resolution: str,
                  generate_audio: bool, timeout: int, poll_interval: int) -> str:
    """Genera un clip y devuelve la URL del MP4 generado."""
    job = submit_video(base_url, api_key, model, prompt, duration, aspect,
                       resolution, generate_audio)
    data = poll_video(base_url, api_key, job, timeout, poll_interval)
    url: str | None = None
    urls = data.get("unsigned_urls")
    if isinstance(urls, list) and urls:
        url = urls[0]
    if not url:
        url = extract_video_url(data)
    if not url:
        # endpoint de contenido oficial: {base}/videos/{id}/content?index=0
        url = f"{base_url}/videos/{job['id']}/content?index=0"
    return url


def download_file(url: str, dest: Path, api_key: str | None = None,
                  base_url: str | None = None) -> None:
    """Descarga el MP4 generado (con token solo si la URL es de la propia API)."""
    headers: dict = {}
    if api_key and base_url and urlparse(url).netloc == urlparse(base_url).netloc:
        headers["Authorization"] = f"Bearer {api_key}"
    dest.parent.mkdir(parents=True, exist_ok=True)
    try:
        with requests.get(url, headers=headers, stream=True, timeout=300) as r:
            r.raise_for_status()
            with open(dest, "wb") as f:
                for chunk in r.iter_content(chunk_size=1 << 20):
                    f.write(chunk)
    except requests.RequestException as e:
        raise ApiError(f"No se pudo descargar el vídeo desde {url}: {e}") from e
    size = dest.stat().st_size
    print(f"     ✔ Guardado {dest} ({size / 1e6:.1f} MB)")


def list_video_models(base_url: str, api_key: str) -> None:
    """Lista los modelos de vídeo disponibles en OpenRouter con sus capacidades."""
    models = fetch_video_models(base_url, api_key)
    if not models:
        print("   (no se encontraron modelos o el endpoint no está disponible)")
        return
    for m in sorted(models, key=lambda x: str(x.get("id", ""))):
        extra = []
        durs = m.get("supported_durations")
        resos = m.get("supported_resolutions")
        aspects = m.get("supported_aspect_ratios")
        if durs:
            extra.append(f"duraciones {sorted(int(d) for d in durs)} s")
        if resos:
            extra.append(f"resoluciones {resos}")
        if aspects:
            extra.append(f"aspectos {aspects}")
        if m.get("generate_audio"):
            extra.append("con audio")
        print(f"   - {m.get('id')}" + (f"  ({', '.join(extra)})" if extra else ""))


# --------------------------------------------------------------------------
# Restricciones legales / política de contenido (no interrumpen el vídeo)
# --------------------------------------------------------------------------
RESTRICTION_KEYWORDS = (
    "policy", "polic", "safety", "safe", "rai", "restrict", "copyright",
    "trademark", "infring", "prohibit", "not allowed", "not permitted",
    "banned", "moderat", "content filter", "filtered", "inappropriate",
    "harmful", "explicit", "nsfw", "sexual", "illegal", "protected", "owned",
    "third-party", "violat", "terms of service", "guideline", "disallowed",
    "community standard", "sensitive", "intellectual property",
)


def is_restriction_error(exc: Exception) -> bool:
    """¿El error es un rechazo por política de contenido / restricción legal?"""
    text = str(exc).lower()
    return any(k in text for k in RESTRICTION_KEYWORDS)


REPHRASE_TEMPLATE = """\
El siguiente prompt de generación de vídeo fue rechazado por una restricción \
legal o de política de contenido:

{prompt}

Motivo del rechazo: {reason}

Reescribe el prompt para que cumpla la política de la plataforma: sin \
personajes, marcas, música, obras o contenido protegido por derechos; contenido \
apto para todos los públicos. Conserva la intención, el sujeto, la acción, la \
cámara y el estilo de la escena. Responde SOLO con el nuevo prompt en inglés, \
sin explicaciones ni comillas.
"""


def rephrase_prompt(base_url: str, api_key: str, llm_model: str,
                    prompt: str, reason: str) -> str | None:
    """Pide al LLM una versión del prompt que cumpla la política."""
    instruction = REPHRASE_TEMPLATE.format(prompt=prompt, reason=(reason or "")[:500])
    models = [llm_model] if llm_model != "auto" else LLM_FALLBACKS
    for model in models:
        try:
            text = chat_completions(base_url, api_key, model, instruction,
                                    temperature=0.7, json_mode=False)
            text = (text or "").strip().strip('"')
            if text:
                return text
        except Exception:  # noqa: BLE001
            continue
    return None


FONT_CANDIDATES = [
    "C:/Windows/Fonts/arial.ttf",
    "C:/Windows/Fonts/segoeui.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/System/Library/Fonts/Helvetica.ttc",
]


def make_placeholder_clip(ffmpeg: str, dest: Path, duration: int, label: str) -> None:
    """Crea un clip de reserva (fondo + texto + silencio) cuando una escena
    no se puede generar por restricciones, para no interrumpir el montaje."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    vf = "format=yuv420p"
    font_src = next((f for f in FONT_CANDIDATES if Path(f).exists()), None)
    if font_src:
        try:
            # copiar la fuente junto al clip y usar solo el nombre de archivo:
            # un path absoluto tipo C:\... rompe el parser de filtros de FFmpeg
            font_local = dest.parent / "_font_placeholder.ttf"
            shutil.copyfile(font_src, font_local)
            vf = (f"drawtext=fontfile={font_local.name}:text='{label}':"
                  f"fontcolor=white:fontsize=48:x=(w-text_w)/2:y=(h-text_h)/2,") + vf
        except OSError:
            vf = "format=yuv420p"
    cmd = [
        ffmpeg, "-y",
        "-f", "lavfi", "-i", "color=c=0x1a1a2e:s=1280x720:r=24",
        "-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo",
        "-t", str(duration),
        "-vf", vf,
        "-c:v", "libx264", "-pix_fmt", "yuv420p",
        "-c:a", "aac",
        "-shortest",
        str(dest),
    ]
    r = run_cmd(cmd, cwd=dest.parent)
    if r.returncode != 0:
        print(f"     ⚠ No se pudo crear el clip de reserva ({r.stderr[-300:]}); se omite.")
        return
    print(f"     🎬 Clip de reserva creado: {dest}")


# --------------------------------------------------------------------------
# Paso 2b: narración en español latinoamericano (TTS vía OpenRouter)
# --------------------------------------------------------------------------
def auth_headers(api_key: str) -> dict:
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    referer = os.environ.get("OPENROUTER_REFERER") or ""
    if referer:
        headers["HTTP-Referer"] = referer
    title = os.environ.get("OPENROUTER_TITLE") or ""
    if title:
        headers["X-Title"] = title
    return headers


def tts_speech(base_url: str, api_key: str, model: str, text: str,
               voice: str, fmt: str = "mp3") -> bytes:
    """POST {base}/audio/speech (compatible OpenAI); devuelve el audio crudo."""
    payload = {
        "model": model,
        "input": text,
        "voice": voice,
        "response_format": fmt,
    }
    try:
        r = requests.post(f"{base_url}/audio/speech", json=payload,
                          headers=auth_headers(api_key), timeout=180)
    except requests.RequestException as e:
        raise ApiError(f"No se pudo conectar con el TTS: {e}") from e
    if r.status_code >= 400:
        msg = r.text[:400]
        try:
            j = r.json()
            msg = (j.get("error", {}).get("message") if isinstance(j.get("error"), dict) else None) \
                or j.get("message") or msg
        except Exception:
            pass
        raise ApiError(f"TTS HTTP {r.status_code}: {msg}", status=r.status_code)
    if not r.content:
        raise ApiError("El TTS devolvió un audio vacío.")
    return r.content


REPHRASE_NARRATION_TEMPLATE = """\
La siguiente línea de narración fue rechazada por una restricción legal o de \
política de contenido:

"{text}"

Motivo del rechazo: {reason}

Reescribe la narración EN ESPAÑOL (latinoamericano) para que cumpla la política \
de la plataforma: sin marcas, obras, personajes o contenido protegido. Conserva \
el sentido de la frase y la misma brevedad (6-10 palabras). Responde SOLO con \
la nueva narración, sin comillas ni explicaciones.
"""


def rephrase_narration(base_url: str, api_key: str, llm_model: str,
                       text: str, reason: str) -> str | None:
    """Pide al LLM una narración que cumpla la política."""
    instruction = REPHRASE_NARRATION_TEMPLATE.format(text=text, reason=(reason or "")[:500])
    models = [llm_model] if llm_model != "auto" else LLM_FALLBACKS
    for model in models:
        try:
            new = chat_completions(base_url, api_key, model, instruction,
                                   temperature=0.7, json_mode=False)
            new = (new or "").strip().strip('"')
            if new:
                return new
        except Exception:  # noqa: BLE001
            continue
    return None


def generate_narration(base_url: str, api_key: str, tts_model: str, voice: str,
                       text: str, llm_model: str, retries: int) -> bytes | None:
    """Genera el audio de narración; ante restricciones reescribe el texto y
    reintenta. Devuelve None si no se puede (el clip irá en silencio)."""
    for attempt in range(1, retries + 1):
        try:
            return tts_speech(base_url, api_key, tts_model, text, voice)
        except ApiError as e:
            if not is_restriction_error(e):
                raise
            print(f"     ⚠ Restricción en la narración (intento {attempt}/{retries}): "
                  f"{str(e)[:150]}")
            new_text = rephrase_narration(base_url, api_key, llm_model, text, str(e))
            if new_text and new_text != text:
                text = new_text
                print(f"     ✍ Narración reescrita: {text}")
                continue
            break
    return None


# --------------------------------------------------------------------------
# Paso 3: montaje con FFmpeg
# --------------------------------------------------------------------------
def lengthen_clip(ffmpeg: str, src: Path, dst: Path, target: float, probe: dict) -> None:
    """Alarga un clip en cámara lenta hasta la duración objetivo (solo si se pide)."""
    actual = probe.get("duration")
    if not actual or actual <= 0:
        raise RuntimeError(f"No se pudo medir la duración de {src.name}.")
    factor = target / actual
    if abs(factor - 1.0) < 0.02:
        shutil.copyfile(src, dst)
        return
    print(f"     ⏱ Alargando {src.name} de {actual:.1f}s a {target:.1f}s (cámara lenta x{factor:.2f})")
    vf = f"[0:v]setpts={factor:.6f}*PTS[v]"
    cmd = [ffmpeg, "-y", "-i", str(src)]
    if probe.get("has_audio"):
        atempo = 1.0 / factor
        vf += f";[0:a]atempo={atempo:.6f}[a]"
        cmd += ["-filter_complex", vf, "-map", "[v]", "-map", "[a]"]
    else:
        cmd += ["-filter_complex", vf, "-map", "[v]"]
    cmd += ["-c:v", "libx264", "-crf", "18", "-preset", "medium"]
    if probe.get("has_audio"):
        cmd += ["-c:a", "aac", "-b:a", "192k"]
    cmd.append(str(dst))
    r = run_cmd(cmd)
    if r.returncode != 0:
        raise RuntimeError(f"FFmpeg no pudo alargar {src.name}: {r.stderr[-500:]}")


# --------------------------------------------------------------------------
# Subtítulos (quemados en el vídeo + archivo .srt)
# --------------------------------------------------------------------------
def format_srt_ts(seconds: float) -> str:
    """Formatea segundos como HH:MM:SS,mmm para SRT."""
    ms = int(round(seconds * 1000))
    h, rem = divmod(ms, 3_600_000)
    m, rem = divmod(rem, 60_000)
    s, ms = divmod(rem, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def wrap_text(text: str, width: int = 42) -> str:
    """Parte el texto en líneas de <= width caracteres (para mejor render)."""
    words = str(text).split()
    lines: list[str] = []
    cur = ""
    for w in words:
        if not cur:
            cur = w
        elif len(cur) + 1 + len(w) <= width:
            cur += " " + w
        else:
            lines.append(cur)
            cur = w
    if cur:
        lines.append(cur)
    return "\n".join(lines)


def build_scene_srt(text: str, duration: float, path: Path,
                    start: float = 0.3, end_pad: float = 0.25) -> None:
    """Escribe el .srt de una escena (subtítulo con el texto de la narración)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    end = max(start + 0.3, duration - end_pad)
    content = (
        "1\n"
        f"{format_srt_ts(start)} --> {format_srt_ts(end)}\n"
        f"{wrap_text(text)}\n"
    )
    path.write_text(content, encoding="utf-8")


def build_combined_srt(entries: list[tuple[str, float, float]], path: Path) -> None:
    """Escribe el .srt del vídeo completo (timecodes acumulados por escena)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    blocks = []
    for idx, (text, start, end) in enumerate(entries, start=1):
        blocks.append(
            f"{idx}\n{format_srt_ts(start)} --> {format_srt_ts(end)}\n{wrap_text(text)}"
        )
    path.write_text("\n\n".join(blocks) + "\n", encoding="utf-8")


def make_demo_srt(dest: Path, text: str, duration: int) -> Path:
    """Genera el .srt de demostración (mismo formato que el real)."""
    build_scene_srt(text, float(duration), dest)
    return dest


def normalize_clip(ffmpeg: str, src: Path, dst: Path, width: int, height: int,
                   fps: int, has_audio: bool, narration: Path | None = None,
                   duration: int | None = None, subtitles: Path | None = None,
                   logo: Path | None = None) -> None:
    """Re-codifica un clip a códec/resolución/fps/audio comunes para concatenarlo.

    Si se pasa `narration`, esa pista de audio sustituye al audio del clip
    (se rellena con silencio y se ajusta a `duration` segundos exactos).
    Si se pasa `subtitles`, el texto se quema en el vídeo (filtro subtitles).
    Si se pasa `logo`, la imagen se superpone pequeña en la esquina superior
    izquierda (filtro overlay).
    """
    vf = (
        f"scale={width}:{height}:force_original_aspect_ratio=decrease,"
        f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2:color=black,"
        f"fps={fps},format=yuv420p"
    )
    if subtitles:
        # copiar el .srt junto al clip y usar solo el nombre de archivo:
        # un path absoluto tipo C:\... rompe el parser de filtros de FFmpeg
        srt_local = dst.parent / f"{dst.stem}.srt"
        shutil.copyfile(subtitles, srt_local)
        vf += (f",subtitles=filename={srt_local.name}"
               f":force_style='FontSize=24,Outline=1,MarginV=30'")
    cmd = [ffmpeg, "-y", "-i", str(src)]
    if narration:
        cmd += ["-i", str(narration)]
    elif not has_audio:
        cmd += ["-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo"]
    if logo:
        cmd += ["-i", str(logo)]
        # índice del input del logo: 0=clip, 1=narración/silencio (si existe)
        logo_idx = 1 + (1 if (narration or not has_audio) else 0)
    if logo:
        logo_h = max(24, round(height * 0.08))  # logo pequeño: ~8% de la altura
        fc = (f"[0:v]{vf}[base];"
              f"[{logo_idx}:v]scale=-2:{logo_h}[lg];"
              f"[base][lg]overlay=10:10[vout]")
        cmd += ["-filter_complex", fc, "-map", "[vout]"]
    else:
        cmd += ["-map", "0:v:0", "-vf", vf]
    if narration:
        cmd += ["-map", "1:a:0", "-af", "apad", "-t", str(duration)]
    elif has_audio:
        cmd += ["-map", "0:a:0"]
    else:
        cmd += ["-map", "1:a:0", "-shortest"]
    cmd += [
        "-c:v", "libx264", "-crf", "18", "-preset", "medium",
        "-c:a", "aac", "-b:a", "192k", "-ar", "48000", "-ac", "2",
        str(dst),
    ]
    r = run_cmd(cmd, cwd=dst.parent)
    if r.returncode != 0:
        raise RuntimeError(f"FFmpeg no pudo normalizar {src.name}: {r.stderr[-500:]}")


def concat_clips(ffmpeg: str, clips: list[Path], out_path: Path, cwd: Path) -> None:
    """Concatena los clips normalizados en un único MP4 (demuxer concat + re-codificación)."""
    list_file = cwd / "concat.txt"
    list_file.write_text("".join(f"file '{p.as_posix()}'\n" for p in clips), encoding="utf-8")
    cmd = [
        ffmpeg, "-y",
        "-f", "concat", "-safe", "0", "-i", "concat.txt",
        "-c:v", "libx264", "-crf", "18", "-preset", "medium",
        "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", "192k", "-ar", "48000", "-ac", "2",
        "-movflags", "+faststart",
        str(out_path),
    ]
    r = run_cmd(cmd, cwd=cwd)
    if r.returncode != 0:
        raise RuntimeError(f"FFmpeg no pudo concatenar los clips: {r.stderr[-500:]}")


def target_dims(resolution: str, aspect: str, first_info: dict) -> tuple[int, int]:
    """Calcula ancho x alto (pares) para la normalización.

    "1080p" se interpreta como el lado corto del fotograma:
    16:9 → 1920x1080 · 9:16 → 1080x1920 · 1:1 → 1080x1080 · 21:9 → 2520x1080.
    """
    if resolution == "auto":
        w = first_info.get("width") or 1920
        h = first_info.get("height") or 1080
    else:
        short = int(re.sub(r"\D", "", resolution) or 1080)
        ar_w, ar_h = (int(x) for x in aspect.split(":"))
        if ar_w >= ar_h:
            w, h = round(short * ar_w / ar_h), short
        else:
            w, h = short, round(short * ar_h / ar_w)
    return w - w % 2, h - h % 2


def fmt_ts(seconds: float) -> str:
    """Formatea segundos como mm:ss."""
    m, s = divmod(int(seconds), 60)
    return f"{m:02d}:{s:02d}"


# --------------------------------------------------------------------------
# Modo demo (sin API)
# --------------------------------------------------------------------------
def make_demo_clips(ffmpeg: str, out_dir: Path, duration: int, count: int = DEFAULT_CLIPS) -> list[Path]:
    """Genera `count` clips sintéticos con FFmpeg para probar el pipeline sin clave API."""
    out_dir.mkdir(parents=True, exist_ok=True)
    sources = [
        "testsrc2=size=640x360:rate=24",
        "smptebars=size=640x360:rate=24",
        "rgbtestsrc=size=640x360:rate=24",
        "testsrc=size=640x360:rate=24",
        "mandelbrot=size=640x360:rate=24",
        "life=size=640x360:rate=24:mold=10:ratio=0.1:death_color=#C83232:life_color=#00ff00",
    ]
    freqs = [330, 392, 440, 494, 523, 587]
    clips: list[Path] = []
    for i in range(1, count + 1):
        src = sources[(i - 1) % len(sources)]
        freq = freqs[(i - 1) % len(freqs)]
        p = out_dir / f"demo_clip_{i}.mp4"
        cmd = [
            ffmpeg, "-y",
            "-f", "lavfi", "-i", src,
            "-f", "lavfi", "-i", f"sine=frequency={freq}:duration={duration}",
            "-t", str(duration),
            "-c:v", "libx264", "-pix_fmt", "yuv420p",
            "-c:a", "aac",
            "-shortest",
            str(p),
        ]
        r = run_cmd(cmd)
        if r.returncode != 0:
            raise RuntimeError(f"FFmpeg no pudo crear el clip demo {i}: {r.stderr[-500:]}")
        clips.append(p)
    return clips


def make_demo_narration(ffmpeg: str, dest: Path, duration: int) -> Path:
    """Genera un audio sintético (tono) para simular la narración en modo demo."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        ffmpeg, "-y",
        "-f", "lavfi", "-i", f"sine=frequency=880:duration={duration}",
        "-t", str(duration),
        "-c:a", "libmp3lame", "-b:a", "96k",
        str(dest),
    ]
    r = run_cmd(cmd)
    if r.returncode != 0:
        raise RuntimeError(f"FFmpeg no pudo crear la narración demo {dest.name}: {r.stderr[-400:]}")
    return dest


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="main.py",
        description="Crea 6 clips de 5 s con la API de OpenRouter (bytedance/seedance-2.0-mini, "
                    "google/veo-3.1…), con narración en español latinoamericano (TTS), y los "
                    "une en un MP4 con FFmpeg.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    ap.add_argument("idea", nargs="?", help="La idea del vídeo (también se puede pasar con --idea).")
    ap.add_argument("--idea", dest="idea_opt", help="La idea del vídeo.")
    ap.add_argument("--script", default=None,
                    help="Guion propio: ruta de archivo o texto. Puede ser JSON con "
                         "{\"scenes\": [{\"prompt\", \"narration\", \"duration\"}, …]} "
                         "(se usa tal cual) o un guion en texto libre (se convierte con "
                         "el LLM). Si no se da idea ni guion, se pregunta.")
    ap.add_argument("--clips", type=int, default=DEFAULT_CLIPS, help="Número de clips/escenas.")
    ap.add_argument("--duration", type=int, default=DEFAULT_DURATION,
                    help="Duración base en segundos. Con narración activa, cada clip se "
                         "ajusta a la duración real de su narración (variable).")
    ap.add_argument("--fixed-duration", action="store_true",
                    help="Con narración activa, usa siempre --duration en vez de ajustar "
                         "cada clip a su narración.")
    ap.add_argument("--model", default=DEFAULT_VIDEO_MODEL,
                    help="Modelo de vídeo de OpenRouter (p. ej. bytedance/seedance-2.0-mini, "
                         "google/veo-3.1, google/veo-3.1-fast).")
    ap.add_argument("--llm-model", default=DEFAULT_LLM_MODEL,
                    help="Modelo LLM para el storyboard ('auto' elige uno disponible).")
    ap.add_argument("--base-url", default=None,
                    help="URL base de la API de OpenRouter (por defecto, la oficial).")
    ap.add_argument("--aspect-ratio", default=None, metavar="VALOR",
                    help="Proporciones del vídeo: horizontal, vertical, cuadrado "
                         "(o 16:9, 9:16, 1:1). Si no se indica, se pregunta al ejecutar.")
    ap.add_argument("--resolution", default="1080p",
                    help="Resolución de salida: 720p, 1080p o 'auto' (usa la del primer clip).")
    ap.add_argument("--fps", type=int, default=24, help="Fotogramas por segundo del vídeo final.")
    ap.add_argument("--out-dir", default="out", help="Carpeta de salida (clips y vídeo final).")
    ap.add_argument("--final-name", default="final_video.mp4", help="Nombre del MP4 final.")
    ap.add_argument("--lengthen", action="store_true",
                    help="Si el modelo no llega a --duration (p. ej. Veo, máx 8 s), alarga el "
                         "clip en cámara lenta con FFmpeg hasta la duración pedida.")
    ap.add_argument("--audio", action="store_true",
                    help="Pide audio generado junto al vídeo (generate_audio). Con narración "
                         "activa, la narración sustituye a este audio.")
    ap.add_argument("--no-narration", action="store_true",
                    help="No generar narración (TTS): los clips van en silencio o con su audio.")
    ap.add_argument("--no-subtitles", action="store_true",
                    help="No quemar subtítulos en el vídeo (por defecto se queman los de la "
                         "narración y se genera subtitles.srt).")
    ap.add_argument("--logo", default=None, metavar="RUTA",
                    help="Imagen de logo (png/jpg…) que se superpone pequeña en la esquina "
                         "superior izquierda del PRIMER clip.")
    ap.add_argument("--tts-model", default=DEFAULT_TTS_MODEL,
                    help="Modelo de texto-a-voz (TTS) de OpenRouter.")
    ap.add_argument("--voice", default=DEFAULT_TTS_VOICE,
                    help="Voz TTS (por defecto: masculina, español latinoamericano).")
    ap.add_argument("--retries", type=int, default=3,
                    help="Intentos por clip/narración ante rechazos por política/restricciones.")
    ap.add_argument("--no-placeholder", action="store_true",
                    help="Si un clip se rechaza por restricciones tras --retries, omitirlo "
                         "en lugar de crear un clip de reserva.")
    ap.add_argument("--no-storyboard", action="store_true",
                    help="No usar LLM para el guion: usa plantillas simples.")
    ap.add_argument("--demo", action="store_true",
                    help="Modo demostración: crea clips sintéticos con FFmpeg (no requiere API).")
    ap.add_argument("--list-models", action="store_true",
                    help="Lista los modelos de vídeo disponibles en OpenRouter y sale.")
    ap.add_argument("--ffmpeg", default="ffmpeg", help="Ruta del binario ffmpeg.")
    ap.add_argument("--ffprobe", default="ffprobe", help="Ruta del binario ffprobe.")
    ap.add_argument("--poll-interval", type=int, default=POLL_INTERVAL,
                    help="Segundos entre comprobaciones del estado de generación.")
    ap.add_argument("--timeout", type=int, default=CLIP_TIMEOUT,
                    help="Tiempo máximo de espera por clip (segundos).")
    return ap


def main(argv: list[str] | None = None, api_key: str | None = None) -> int:
    """Ejecuta el pipeline completo.

    `api_key` permite pasar la clave de OpenRouter de forma explícita (p. ej.
    desde la interfaz Streamlit, una clave distinta por sesión); si es None se
    usa la variable de entorno o el archivo .env.
    """
    global POLL_INTERVAL, CLIP_TIMEOUT
    args = build_parser().parse_args(argv)
    POLL_INTERVAL = args.poll_interval
    CLIP_TIMEOUT = args.timeout

    print(BANNER)
    ffmpeg = shutil.which(args.ffmpeg) or args.ffmpeg
    ffprobe = shutil.which(args.ffprobe) or args.ffprobe
    if shutil.which(args.ffmpeg) is None:
        print("⚠ No se encontró ffmpeg. Instálalo (https://ffmpeg.org) o usa --ffmpeg <ruta>.")
        return 2

    # Proporciones: se preguntan si no se indicaron por línea de comandos
    if args.aspect_ratio is None:
        if sys.stdin.isatty():
            try:
                resp = input("¿Proporciones del vídeo? (h)orizontal / (v)ertical / "
                             "(c)uadrado [h]: ").strip().lower()
            except EOFError:
                resp = ""
            args.aspect_ratio = resp or "h"
        else:
            args.aspect_ratio = "h"
    args.aspect_ratio = normalize_aspect(args.aspect_ratio)

    # Logo opcional (solo en el primer clip)
    logo_path: Path | None = None
    if args.logo:
        if Path(args.logo).exists():
            logo_path = Path(args.logo).resolve()
            print(f"🖼️ Logo: {logo_path} (esquina superior izquierda del primer clip)")
        else:
            print(f"⚠ No se encontró el logo '{args.logo}'; se continúa sin logo.")

    out_dir = Path(args.out_dir).resolve()
    clips_dir = out_dir / "clips"
    norm_dir = out_dir / "clips_norm"
    final_path = out_dir / args.final_name

    env = load_dotenv()
    api_key = api_key or get_api_key(env)
    base_url = args.base_url or get_base_url(env).rstrip("/")

    if args.list_models:
        try:
            print(f"Modelos de vídeo disponibles en OpenRouter ({base_url}):")
            list_video_models(base_url, api_key or "")
        except ApiError as e:
            print(f"No se pudieron listar los modelos: {e}")
        return 0

    # ---- Paso 0: idea + storyboard --------------------------------------
    if args.demo:
        print(f"▶ Modo demo: se generarán {args.clips} clips sintéticos con FFmpeg (sin API).")
        demo_dir = out_dir / "demo"
        clips = make_demo_clips(ffmpeg, demo_dir, args.duration, args.clips)
        scenes = [
            {"prompt": f"Demo {i}",
             "narration": f"Escena de demostración número {i}.",
             "duration": args.duration}
            for i in range(1, args.clips + 1)
        ]
        variable = False
        effective_duration = args.duration
        subtitles_on = (not args.no_subtitles) and (not args.no_narration)
        clips_meta: list[tuple[Path, Path | None, int, Path | None]] = []
        for i in range(1, args.clips + 1):
            narr = None
            if not args.no_narration:
                narr = make_demo_narration(ffmpeg, demo_dir / f"demo_narr_{i}.mp3", args.duration)
            srt = None
            if subtitles_on and scenes[i - 1].get("narration"):
                srt = make_demo_srt(out_dir / "subtitles" / f"clip_{i:02d}.srt",
                                    scenes[i - 1]["narration"], args.duration)
            clips_meta.append((clips[i - 1], narr, args.duration, srt))
    else:
        if not api_key:
            print(
                "No se encontró OPENROUTER_API_KEY. Copia .env.example a .env y pega tu clave "
                "(https://openrouter.ai/settings/keys), o define la variable de entorno. "
                "Recuerda que OpenRouter requiere saldo/créditos en la cuenta."
            )
            return 2

        # Idea o guion del usuario (--script acepta ruta de archivo o texto)
        idea = (args.idea_opt or args.idea or "").strip()
        script_src: str | None = None
        if args.script:
            if Path(args.script).exists():
                script_src = Path(args.script).read_text(encoding="utf-8").strip()
                print(f"📜 Guion leído del archivo: {args.script}")
            else:
                script_src = args.script.strip()
        if not idea and not script_src:
            try:
                idea = input("💡 ¿Cuál es tu idea para el vídeo? (o escribe 'guion:' "
                             "seguido de tu guion) ").strip()
            except EOFError:
                idea = ""
            if idea.lower().startswith("guion:"):
                script_src = idea.split(":", 1)[1].strip()
                idea = ""
        if not idea and not script_src:
            print("No se proporcionó ninguna idea ni guion. Usa: python main.py \"tu idea\" "
                  "o --script <guion.json|texto>")
            return 2

        if script_src:
            print("📜 Usando el guion del usuario…")
            # 1) ¿JSON de escenas? (se aceptan tantas escenas como traiga el guion)
            scenes = parse_script_scenes(script_src)
            if scenes:
                if len(scenes) != args.clips:
                    print(f"     ℹ El guion trae {len(scenes)} escenas; se usarán "
                          f"{len(scenes)} clips (--clips {args.clips} ignorado).")
                args.clips = len(scenes)
            else:
                # 2) guion en texto libre: convertirlo con el LLM
                if args.no_storyboard:
                    print("     ℹ --no-storyboard activo: no se puede convertir un guion "
                          "de texto libre. Pega el guion como JSON de escenas.")
                    print("❌ No se pudo interpretar el guion.")
                    return 2
                print("📝 Convirtiendo el guion en escenas con OpenRouter…")
                scenes = script_to_scenes(base_url, api_key, script_src, args.duration,
                                          args.llm_model, args.clips)
            if scenes is None:
                print("❌ No se pudo interpretar el guion. Posibles causas:")
                print("   - No es JSON válido con 'scenes' (o una lista de escenas).")
                print("   - Es texto libre y la conversión con el LLM falló (revisa la "
                      "clave/el saldo en https://openrouter.ai/settings/credits).")
                print("   - Formato esperado: {\"scenes\": [{\"prompt\": \"…\", "
                      "\"narration\": \"…\", \"duration\": 5}, …]}")
                return 2
        else:
            print(f"📝 Idea: {idea}")
            if args.no_storyboard:
                print("📝 Storyboard: plantillas simples (--no-storyboard).")
                scenes = naive_storyboard(idea, args.duration, args.clips)
            else:
                print(f"📝 Convirtiendo la idea en {args.clips} escenas con OpenRouter…")
                scenes = storyboard_via_openrouter(base_url, api_key, idea, args.duration,
                                                   args.llm_model, args.clips)
                if not scenes:
                    print("⚠ No se pudo usar el LLM para el storyboard; uso plantillas simples.")
                    scenes = naive_storyboard(idea, args.duration, args.clips)
        for i, s in enumerate(scenes, 1):
            extra = f"  🎙 {s.get('narration')}" if s.get("narration") else ""
            dur = f"  ⏱ {s.get('duration')} s" if s.get("duration") else ""
            print(f"     [{i}] {s['prompt'][:80]}{'…' if len(s['prompt']) > 80 else ''}{extra}{dur}")

        # Duración y parámetros: se ajustan a los metadatos reales del modelo.
        aspect, resolution = adjust_params(base_url, api_key, args.model,
                                           args.aspect_ratio, args.resolution)
        args.aspect_ratio, args.resolution = aspect, resolution  # el montaje usa los valores ajustados
        # Con narración activa, cada clip dura lo que tarda su narración (variable);
        # con --no-narration o --fixed-duration, todos duran --duration.
        variable = (not args.no_narration) and not args.fixed_duration
        subtitles_on = (not args.no_subtitles) and (not args.no_narration)
        if variable:
            print(f"🎬 Generando {args.clips} clips de duración variable "
                  f"(cada uno se ajusta a su narración) con '{args.model}' vía OpenRouter…")
        else:
            effective_duration = resolve_duration(base_url, api_key, args.model, args.duration)
            print(f"🎬 Generando {args.clips} clips de {effective_duration} s con "
                  f"'{args.model}' vía OpenRouter…")

        # ---- Paso 2: generar narración + clips (duración variable) ---------
        clips_meta = []
        for i, scene in enumerate(scenes, 1):
            print(f"     Escena {i}/{args.clips}: narración y clip…")
            # 1) narración TTS primero, para medir su duración real
            narration_file: Path | None = None
            narration_dur: float | None = None
            if not args.no_narration and scene.get("narration"):
                print(f"     🎙 Narración {i}/{args.clips}: {scene['narration']}")
                data = generate_narration(base_url, api_key, args.tts_model, args.voice,
                                          scene["narration"], args.llm_model, args.retries)
                if data:
                    nf = out_dir / "narration" / f"narration_{i:02d}.mp3"
                    nf.parent.mkdir(parents=True, exist_ok=True)
                    nf.write_bytes(data)
                    narration_file = nf
                    info = probe_video(ffprobe, nf)
                    narration_dur = info.get("duration")
                    dur_txt = f"{narration_dur:.1f} s" if narration_dur else "¿?"
                    print(f"     ✔ Narración guardada ({dur_txt})")
                else:
                    print(f"     ⚠ Sin narración para el clip {i} (restricciones); irá en silencio.")

            # 2) duración del clip: se ajusta a la narración (variable) o al guion
            if variable:
                cdur = clip_duration_for(base_url, api_key, args.model, narration_dur,
                                         scene.get("duration"), args.duration)
            else:
                cdur = effective_duration
            print(f"     ⏱ Duración del clip: {cdur} s"
                  + (f"  (narración {narration_dur:.1f} s)" if narration_dur else ""))

            # subtítulos de la escena (texto de la narración)
            srt_file: Path | None = None
            if subtitles_on and scene.get("narration"):
                srt_file = out_dir / "subtitles" / f"clip_{i:02d}.srt"
                build_scene_srt(scene["narration"], float(cdur), srt_file)

            # 3) generar el vídeo con esa duración
            prompt = scene["prompt"]
            use_audio = args.audio and args.no_narration
            dest = clips_dir / f"clip_{i:02d}.mp4"
            generated = False
            attempt = 0
            while attempt < args.retries:
                attempt += 1
                try:
                    url = generate_clip(base_url, api_key, args.model, prompt,
                                        cdur, aspect, resolution,
                                        use_audio, args.timeout, args.poll_interval)
                    download_file(url, dest, api_key, base_url)
                    generated = True
                    break
                except ApiError as e:
                    if not is_restriction_error(e):
                        raise  # error no relacionado con restricciones: sí detiene
                    print(f"     ⚠ Restricción legal/política (intento {attempt}/{args.retries}): "
                          f"{str(e)[:180]}")
                    # 1) reescribir el prompt para cumplir la política
                    new_prompt = rephrase_prompt(base_url, api_key, args.llm_model,
                                                 prompt, str(e))
                    if new_prompt and new_prompt != prompt:
                        prompt = new_prompt
                        print("     ✍ Prompt reescrito para cumplir la política; reintentando…")
                        continue
                    # 2) si la restricción venía del audio, reintentar sin audio
                    if use_audio:
                        use_audio = False
                        print("     🔇 Reintento sin audio generado; reintentando…")
                        continue
                    break  # sin más opciones: clip de reserva
            if not generated:
                print(f"     ⚠ El clip {i} no se pudo generar por restricciones "
                      f"tras {attempt} intento(s).")
            if generated or not args.no_placeholder:
                if not generated:
                    make_placeholder_clip(ffmpeg, dest, cdur, f"Escena {i}")
                clips_meta.append((dest, narration_file, cdur, srt_file))
            else:
                print("     (--no-placeholder: este clip se omite.)")
            time.sleep(2)  # margen entre llamadas para evitar límites de velocidad

        if not clips_meta:
            print("❌ Ningún clip se generó; no se puede montar el vídeo.")
            return 2

    # guion con indicaciones temporales (timecodes acumulados; también en demo)
    print("\n📋 Guion con indicaciones temporales:")
    t0 = 0.0
    srt_entries: list[tuple[str, float, float]] = []
    for i, (dest, narr, cdur, srt) in enumerate(clips_meta, 1):
        t1 = t0 + cdur
        nar = scenes[i - 1].get("narration") if i - 1 < len(scenes) else None
        print(f"     Escena {i:>2}  {fmt_ts(t0)} – {fmt_ts(t1)}  ({cdur} s)"
              + (f"  🎙 {nar}" if nar else ""))
        if subtitles_on and nar:
            srt_entries.append((nar, t0, t1))
        t0 = t1
    if subtitles_on and srt_entries:
        combined = out_dir / "subtitles.srt"
        build_combined_srt(srt_entries, combined)
        print(f"     💬 Subtítulos del vídeo completo: {combined}")

    # ---- Paso 3: verificar, alargar si hace falta, normalizar y unir -------
    processed: list[tuple[Path, Path | None, int, Path | None]] = []
    for i, (p, narr, cdur, srt) in enumerate(clips_meta, 1):
        info = probe_video(ffprobe, p)
        dur = info.get("duration")
        dur_txt = f"{dur:.1f} s" if dur else "desconocida"
        print(f"     clip_{i:02d}.mp4 → duración {dur_txt}, {info.get('width')}x{info.get('height')}"
              + (" + 🎙 narración" if narr else "") + (" + 💬 subtítulos" if srt else ""))
        if args.lengthen and not variable and dur and args.duration - dur > 0.5 \
                and args.duration > cdur:
            target = clips_dir / f"clip_{i:02d}_lengthened.mp4"
            lengthen_clip(ffmpeg, p, target, float(args.duration), info)
            processed.append((target, narr, args.duration, srt))
        else:
            processed.append((p, narr, cdur, srt))

    first_info = probe_video(ffprobe, processed[0][0])
    width, height = target_dims(args.resolution, args.aspect_ratio, first_info)

    norm_dir.mkdir(parents=True, exist_ok=True)
    print("🔧 Normalizando clips con FFmpeg (mismo códec/resolución/fps/audio)…")
    norm_clips: list[Path] = []
    for i, (p, narr, cdur, srt) in enumerate(processed, 1):
        n = norm_dir / f"clip_{i:02d}_norm.mp4"
        info = probe_video(ffprobe, p)
        normalize_clip(ffmpeg, p, n, width, height, args.fps, info.get("has_audio", False),
                       narration=narr, duration=cdur if narr else None, subtitles=srt,
                       logo=logo_path if i == 1 else None)
        norm_clips.append(n)

    print(f"🧩 Concatenando los {len(norm_clips)} clips en un único MP4…")
    concat_clips(ffmpeg, norm_clips, final_path, out_dir)

    final_info = probe_video(ffprobe, final_path)
    print("\n✅ ¡Listo!")
    print(f"   Vídeo final: {final_path}")
    if final_info.get("duration"):
        print(f"   Duración total: {final_info['duration']:.1f} s")
    print(f"   Resolución: {width}x{height} @ {args.fps} fps")
    print(f"   Clips individuales en: {clips_dir}")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\nInterrumpido por el usuario.")
        sys.exit(130)
