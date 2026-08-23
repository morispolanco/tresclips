# -*- coding: utf-8 -*-
"""
TresClips 🎬 — Interfaz web (Streamlit)

Usa main.py como motor: guion con indicaciones temporales, generación de clips
de duración variable con OpenRouter, narración en español latinoamericano (TTS)
y montaje FFmpeg.

Ejecutar con:
    streamlit run app.py
"""

from __future__ import annotations

import io
import queue
import re
import shutil
import sys
import tempfile
import threading
import uuid
from pathlib import Path

import streamlit as st

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
import main as m  # noqa: E402

st.set_page_config(page_title="TresClips 🎬", page_icon="🎬", layout="wide")

GUION_RE = re.compile(
    r"^\s*Escena\s+(\d+)\s+(\d\d:\d\d)\s*–\s*(\d\d:\d\d)\s+\((\d+)\s*s\)(?:\s+🎙\s*(.*))?$"
)
PROGRESS_RE = re.compile(r"^\s*Escena (\d+)/(\d+):")


class QueueStream(io.TextIOBase):
    """Captura print() (de main.py) y lo encola por líneas para la UI."""

    def __init__(self, q: "queue.Queue[object]"):
        self.q = q
        self.buf = ""

    def write(self, s: str) -> int:
        self.buf += s
        while "\n" in self.buf:
            line, self.buf = self.buf.split("\n", 1)
            self.q.put(line)
        return len(s)

    def flush(self) -> None:
        pass


def worker(argv: list[str], q: "queue.Queue[object]", api_key: str) -> None:
    """Ejecuta main.main() en segundo plano capturando su salida.

    La clave se pasa por parámetro (no por variable global) para que cada sesión
    use la suya aunque haya usuarios concurrentes en el mismo contenedor."""
    old_out, old_err = sys.stdout, sys.stderr
    stream = QueueStream(q)
    try:
        sys.stdout = stream
        sys.stderr = stream
        code = m.main(argv, api_key=api_key or None)
    except Exception as e:  # noqa: BLE001
        print(f"ERROR inesperado: {type(e).__name__}: {e}")
        code = 2
    finally:
        sys.stdout, sys.stderr = old_out, old_err
        q.put(("__EXIT__", code))


def session_run_dir() -> Path:
    """Directorio único por sesión (el contenedor de la nube es compartido)."""
    if "run_dir" not in st.session_state:
        base = Path(tempfile.gettempdir()) / "tresclips"
        base.mkdir(parents=True, exist_ok=True)
        st.session_state["run_dir"] = str(base / uuid.uuid4().hex[:10])
    return Path(st.session_state["run_dir"])


def build_argv(cfg: dict, demo: bool) -> list[str]:
    argv: list[str] = []
    if not demo and cfg.get("script"):
        argv += ["--script", str(cfg["script"])]
    elif not demo and cfg.get("idea"):
        argv += ["--idea", cfg["idea"]]
    argv += ["--clips", str(cfg["clips"]), "--duration", str(cfg["duration"])]
    for flag, key in (
        ("--model", "model"), ("--tts-model", "tts_model"), ("--voice", "voice"),
        ("--aspect-ratio", "aspect"), ("--resolution", "resolution"),
        ("--fps", "fps"), ("--out-dir", "out_dir"), ("--llm-model", "llm_model"),
        ("--logo", "logo"),
    ):
        if cfg.get(key):
            argv += [flag, str(cfg[key])]
    for flag, key in (
        ("--no-narration", "no_narration"), ("--no-subtitles", "no_subtitles"),
        ("--fixed-duration", "fixed_duration"), ("--audio", "audio"),
        ("--lengthen", "lengthen"), ("--no-storyboard", "no_storyboard"),
        ("--no-placeholder", "no_placeholder"),
    ):
        if cfg.get(key):
            argv.append(flag)
    if demo:
        argv.append("--demo")
    return argv


def render_result(result: dict) -> None:
    exit_code = result["exit"]
    lines = result["lines"]
    if exit_code != 0:
        tail = [ln for ln in lines if ln.strip()][-6:]
        detail = "\n".join(tail) if tail else "(sin detalles en el registro)"
        st.error(
            f"❌ El pipeline terminó con error (código {exit_code}).\n\n"
            f"**Últimas líneas del registro:**\n```\n{detail}\n```\n\n"
            "Pulsa en **📜 Registro completo** para ver todo."
        )
    else:
        st.success("✅ ¡Vídeo generado!")
    guion = result.get("guion") or []
    if guion:
        st.markdown("### 📋 Guion con indicaciones temporales")
        st.dataframe(
            {
                "Escena": [g[0] for g in guion],
                "Inicio": [g[1] for g in guion],
                "Fin": [g[2] for g in guion],
                "Duración (s)": [g[3] for g in guion],
                "Narración": [g[4] or "" for g in guion],
            },
            use_container_width=True,
            hide_index=True,
        )
    video = result.get("video")
    if video and Path(video).exists():
        st.markdown("### 🎬 Vídeo final")
        st.video(str(video))
        try:
            with open(video, "rb") as f:
                st.download_button(
                    "⬇️ Descargar MP4",
                    data=f.read(),
                    file_name=Path(video).name,
                    mime="video/mp4",
                    use_container_width=True,
                )
        except OSError as e:
            st.warning(f"No se pudo preparar la descarga: {e}")
        srt = Path(video).parent / "subtitles.srt"
        if srt.exists():
            try:
                with open(srt, "rb") as f:
                    st.download_button(
                        "💬 Descargar subtítulos (.srt)",
                        data=f.read(),
                        file_name="subtitles.srt",
                        mime="application/x-subrip",
                    )
            except OSError:
                pass
    with st.expander("📜 Registro completo", expanded=exit_code != 0):
        st.code("\n".join(lines[-200:]), language=None)


def main() -> None:
    st.title("🎬 TresClips")
    st.caption(
        "Vídeo a partir de una idea: guion con indicaciones temporales, clips de "
        "duración variable, narración en **español latinoamericano masculino** (TTS) "
        "y montaje FFmpeg — todo con tu clave de OpenRouter."
    )

    with st.sidebar:
        st.header("⚙️ Configuración")
        api_key = st.text_input(
            "Clave de OpenRouter", type="password",
            help="Cada usuario pone la suya. Se usa solo en tu sesión y no se "
                 "guarda. Consíguela en https://openrouter.ai/settings/keys "
                 "(requiere saldo). Si la dejas vacía y hay un archivo .env, se usa ese.",
        )
        st.caption("🔒 Tu clave se usa solo en esta sesión y nunca se guarda ni se comparte.")
        st.divider()
        clips = st.slider("Nº de clips", 1, 12, m.DEFAULT_CLIPS)
        duration = st.number_input(
            "Duración base (s)", 2, 30, m.DEFAULT_DURATION,
            help="Con narración activa, cada clip se ajusta a su narración (variable)",
        )
        model = st.text_input("Modelo de vídeo", value=m.DEFAULT_VIDEO_MODEL)
        tts_model = st.text_input("Modelo TTS", value=m.DEFAULT_TTS_MODEL)
        voice = st.text_input(
            "Voz TTS", value=m.DEFAULT_TTS_VOICE,
            help="aura-2-alvaro-es = masculina, español latinoamericano",
        )
        llm_model = st.text_input(
            "Modelo del guion (LLM)", value="deepseek/deepseek-v4-flash-0731",
            help="LLM de OpenRouter que escribe el storyboard/guion",
        )
        logo_file = st.file_uploader(
            "🖼️ Logo (opcional)", type=["png", "jpg", "jpeg", "webp"],
            help="Se superpone pequeño en la esquina superior izquierda del primer clip",
        )
        aspect_label = st.radio(
            "Proporciones",
            ["Horizontal (16:9)", "Vertical (9:16)", "Cuadrado (1:1)"],
            index=0,
            horizontal=True,
            help="Formato del vídeo final (horizontal, vertical o cuadrado)",
        )
        aspect = {
            "Horizontal (16:9)": "16:9",
            "Vertical (9:16)": "9:16",
            "Cuadrado (1:1)": "1:1",
        }[aspect_label]
        resolution = st.selectbox(
            "Resolución", ["auto", "480p", "720p", "1080p", "2K", "4K"], index=3)
        fps = st.selectbox("FPS", [24, 30, 60], index=0)
        col1, col2 = st.columns(2)
        with col1:
            no_narr = st.checkbox("Sin narración", value=False)
            no_sub = st.checkbox("Sin subtítulos", value=False)
            fixed = st.checkbox("Duración fija", value=False)
        with col2:
            audio = st.checkbox("Audio del modelo", value=False)
            lengthen = st.checkbox("Alargar a --duration", value=False)
            no_sb = st.checkbox("Sin storyboard LLM", value=False)
            no_ph = st.checkbox("Omitir clips bloqueados", value=False)
        if st.button("📋 Ver modelos de vídeo", use_container_width=True):
            try:
                models = m.fetch_video_models(
                    m.get_base_url(m.load_dotenv()), api_key or None)
                if models:
                    st.write("**Modelos de vídeo disponibles:**")
                    for mod in sorted(models, key=lambda x: str(x.get("id", ""))):
                        durs = mod.get("supported_durations")
                        extra = f" · duraciones {sorted(int(d) for d in durs)} s" if durs else ""
                        st.code(f"{mod.get('id')}{extra}", language=None)
                else:
                    st.info("No se encontraron modelos (¿clave válida?).")
            except Exception as e:  # noqa: BLE001
                st.error(f"No se pudieron listar modelos: {e}")
        st.divider()
        with st.expander("ℹ️ Cómo funciona"):
            st.markdown(
                "1. **Storyboard**: un LLM de OpenRouter convierte tu idea en escenas "
                "con prompt, narración en español y duración sugerida.\n"
                "2. **Vídeo**: la API de vídeo genera cada clip con la duración que "
                "necesita (se ajusta a la narración).\n"
                "3. **Narración**: TTS en español latinoamericano (voz masculina).\n"
                "4. **Subtítulos**: el texto de la narración se quema en el vídeo y "
                "se genera `subtitles.srt` para el vídeo completo.\n"
                "5. **FFmpeg** mezcla y concatena todo en un MP4.\n\n"
                "💡 Prueba primero con **Modo demo** (no gasta créditos)."
            )

    if shutil.which("ffmpeg") is None:
        st.error(
            "⚠ **ffmpeg no está disponible en este entorno.**\n\n"
            "- En local: instálalo (https://ffmpeg.org) y reinicia la app.\n"
            "- En Streamlit Cloud: el archivo `packages.txt` lo instala "
            "automáticamente al desplegar."
        )

    input_mode = st.radio(
        "¿Qué quieres pegar?", ["💡 Una idea", "📜 Un guion"], horizontal=True)
    if input_mode == "💡 Una idea":
        guion = ""
        idea = st.text_area(
            "💡 Tu idea",
            height=90,
            placeholder="Un robot explorador descubre una ciudad submarina olvidada…",
        )
    else:
        idea = ""
        guion = st.text_area(
            "📜 Tu guion",
            height=220,
            placeholder=(
                'JSON de escenas:\n{"scenes": [{"prompt": "...", "narration": "...", '
                '"duration": 5}, ...]}\n\nO un guion en texto libre (el LLM lo '
                "convertirá en escenas)."
            ),
            help="JSON con la clave 'scenes' (cada escena: prompt, narration, duration) "
                 "o un guion en texto libre que se convierte automáticamente con el LLM.",
        )
    c1, c2 = st.columns(2)
    with c1:
        generar = st.button("🎬 Generar vídeo", type="primary", use_container_width=True)
    with c2:
        demo = st.button("🧪 Modo demo (sin API)", use_container_width=True)

    if generar or demo:
        if not demo and input_mode == "💡 Una idea" and not idea.strip():
            st.warning("Escribe una idea antes de generar.")
            return
        if not demo and input_mode != "💡 Una idea" and not guion.strip():
            st.warning("Pega tu guion antes de generar.")
            return
        cfg = {
            "clips": int(clips), "duration": int(duration),
            "model": model.strip(), "tts_model": tts_model.strip(), "voice": voice.strip(),
            "llm_model": llm_model.strip(),
            "aspect": aspect, "resolution": resolution, "fps": int(fps),
            "no_narration": bool(no_narr), "no_subtitles": bool(no_sub),
            "fixed_duration": bool(fixed), "audio": bool(audio), "lengthen": bool(lengthen),
            "no_storyboard": bool(no_sb), "no_placeholder": bool(no_ph),
        }
        run_dir = session_run_dir()
        cfg["out_dir"] = str(run_dir)
        if not demo:
            if input_mode == "💡 Una idea":
                cfg["idea"] = idea.strip()
            else:
                script_path = run_dir / "guion.txt"
                script_path.write_text(guion, encoding="utf-8")
                cfg["script"] = str(script_path)
            if logo_file is not None:
                logo_dir = run_dir / "logo"
                logo_dir.mkdir(parents=True, exist_ok=True)
                logo_path = logo_dir / (Path(logo_file.name).name or "logo.png")
                logo_path.write_bytes(logo_file.getbuffer())
                cfg["logo"] = str(logo_path)
        argv = build_argv(cfg, demo)
        st.info(f"🚀 Lanzando pipeline… (esto puede tardar varios minutos). "
                f"Archivos en: `{run_dir}`")

        q: "queue.Queue[object]" = queue.Queue()
        thread = threading.Thread(target=worker, args=(argv, q, api_key), daemon=True)
        thread.start()

        lines: list[str] = []
        guion: list[tuple] = []
        exit_code: int | None = None
        with st.status("Ejecutando pipeline…", expanded=True) as status:
            log_box = st.empty()
            prog = st.progress(0.0, text="Arrancando…")
            total_clips = max(1, cfg["clips"])
            while True:
                try:
                    item = q.get(timeout=0.3)
                except queue.Empty:
                    continue
                if isinstance(item, tuple) and item and item[0] == "__EXIT__":
                    exit_code = int(item[1])
                    break
                line = str(item)
                lines.append(line)
                log_box.code("\n".join(lines[-80:]), language=None)
                pm = PROGRESS_RE.match(line)
                if pm:
                    done, total = int(pm.group(1)), int(pm.group(2))
                    prog.progress(min(0.98, done / max(1, total)),
                                  text=f"Escena {done}/{total}…")
                gm = GUION_RE.match(line)
                if gm:
                    guion.append((int(gm.group(1)), gm.group(2), gm.group(3),
                                  int(gm.group(4)), (gm.group(5) or "").strip() or None))
                if "¡Listo!" in line:
                    prog.progress(1.0, text="¡Listo!")
            status.update(label="Pipeline terminado",
                          state="error" if exit_code != 0 else "complete")

        final_path = run_dir / "final_video.mp4"
        st.session_state["last_result"] = {
            "exit": exit_code, "lines": lines, "guion": guion, "video": str(final_path),
        }

    if st.session_state.get("last_result"):
        render_result(st.session_state["last_result"])


if __name__ == "__main__":
    main()
