# -*- coding: utf-8 -*-
"""Tests rápidos de unidades de main.py (sin red ni API)."""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import main as m  # noqa: E402


def test_parse_scenes_json():
    scenes = [
        {"prompt": f"Prompt {i}", "narration": f"Narración {i}", "duration": 4 + (i % 4)}
        for i in range(1, 7)
    ]
    raw = "```json\n" + json.dumps({"scenes": scenes}) + "\n```"
    got = m.parse_scenes_json(raw)
    assert got == scenes, got
    assert m.parse_scenes_json("no json aqui") is None
    assert m.parse_scenes_json(json.dumps({"scenes": scenes[:3]})) is None  # menos de 6
    # narración y duración opcionales por escena
    got2 = m.parse_scenes_json(json.dumps({"scenes": [{"prompt": f"P{i}"} for i in range(1, 7)]}))
    assert len(got2) == 6 and all(s["prompt"] and s["narration"] is None and s["duration"] is None
                                 for s in got2)
    # también acepta una lista JSON directa (guion del usuario)
    bare = m.parse_scenes_json(json.dumps([{"prompt": f"P{i}", "narration": f"N{i}"}
                                           for i in range(1, 7)]))
    assert len(bare) == 6 and bare[0]["narration"] == "N1"
    print("test_parse_scenes_json OK")


def test_naive_storyboard():
    scenes = m.naive_storyboard("un gato astronauta", 5, num_scenes=6)
    assert len(scenes) == 6
    assert all("gato astronauta" in s["prompt"] for s in scenes)
    assert all(s["narration"] for s in scenes)
    assert all(isinstance(s["duration"], int) and 4 <= s["duration"] <= 15 for s in scenes)
    assert len(m.naive_storyboard("x", 5, num_scenes=3)) == 3
    print("test_naive_storyboard OK")


def test_target_dims():
    assert m.target_dims("1080p", "16:9", {}) == (1920, 1080)
    assert m.target_dims("720p", "9:16", {}) == (720, 1280)
    assert m.target_dims("1080p", "1:1", {}) == (1080, 1080)
    assert m.target_dims("auto", "16:9", {"width": 640, "height": 360}) == (640, 360)
    print("test_target_dims OK")


def test_extract_video_url():
    cases = [
        ({"video_url": "https://x/v.mp4"}, "https://x/v.mp4"),
        ({"url": "https://x/v.mp4"}, "https://x/v.mp4"),
        ({"video": {"url": "https://x/v.mp4"}}, "https://x/v.mp4"),
        ({"videos": [{"url": "https://x/v.mp4"}]}, "https://x/v.mp4"),
        ({"output": {"video_url": "https://x/v.mp4"}}, "https://x/v.mp4"),
        ({"status": "completed", "data": {"url": "https://x/v.mp4"}}, "https://x/v.mp4"),
        ({"status": "queued"}, None),
        ({"video": "https://x/v.mp4"}, "https://x/v.mp4"),
        ({"gs://bucket/v.mp4"}, None),  # URLs gs:// no se aceptan
    ]
    for data, expected in cases:
        got = m.extract_video_url(data)
        assert got == expected, (data, got, expected)
    print("test_extract_video_url OK")


def test_resolve_url():
    base = "https://openrouter.ai/api/v1"
    assert m.resolve_url(base, "/api/v1/videos/job-1") == "https://openrouter.ai/api/v1/videos/job-1"
    assert m.resolve_url(base, "https://cdn.example/v.mp4") == "https://cdn.example/v.mp4"
    assert m.resolve_url(base, "videos/job-1") == "https://openrouter.ai/api/v1/videos/job-1"
    print("test_resolve_url OK")


def test_resolve_duration_fallback():
    m._MODEL_META_CACHE = {}  # sin metadatos -> respaldo
    assert m.resolve_duration("http://x/api/v1", "k", "google/veo-3.1", 10) == 8
    assert m.resolve_duration("http://x/api/v1", "k", "google/veo-3.1", 6) == 6
    assert m.resolve_duration("http://x/api/v1", "k", "otro/modelo", 10) == 10
    print("test_resolve_duration_fallback OK")


def test_resolve_duration_round_up_and_clip_duration():
    m._MODEL_META_CACHE = {"http://x": [{"id": "google/veo-3.1", "supported_durations": [4, 6, 8]}]}
    assert m.resolve_duration("http://x", "k", "google/veo-3.1", 5, round_up=True) == 6
    assert m.resolve_duration("http://x", "k", "google/veo-3.1", 9, round_up=True) == 8  # techo máx
    assert m.resolve_duration("http://x", "k", "google/veo-3.1", 6, round_up=True) == 6
    # duración del clip a partir de la narración medida (5.0 s -> 5.75 -> 6)
    assert m.clip_duration_for("http://x", "k", "google/veo-3.1", 5.0, None, 5) == 6
    assert m.clip_duration_for("http://x", "k", "google/veo-3.1", 3.1, None, 5) == 4  # mín 4
    assert m.clip_duration_for("http://x", "k", "google/veo-3.1", None, 7, 5) == 6  # sugerido -> 6
    assert m.clip_duration_for("http://x", "k", "google/veo-3.1", None, None, 5) == 4  # 5 no soportado
    m._MODEL_META_CACHE = {}
    print("test_resolve_duration_round_up_and_clip_duration OK")


def test_is_restriction_error():
    ok = m.is_restriction_error
    assert ok(m.ApiError("Content policy violation: copyrighted material detected"))
    assert ok(m.ApiError("prompt blocked by safety filters"))
    assert ok(m.ApiError("403: not allowed due to RAI restrictions"))
    assert ok(m.ApiError("illegal content: trademarks and third-party IP"))
    assert not ok(m.ApiError("HTTP 429: rate limit exceeded"))
    assert not ok(m.ApiError("HTTP 402: insufficient credits"))
    assert not ok(m.ApiError("connection timed out"))
    assert not ok(m.ApiError("HTTP 500: internal server error"))
    print("test_is_restriction_error OK")


def test_make_placeholder_clip():
    import shutil
    ffmpeg = shutil.which("ffmpeg")
    ffprobe = shutil.which("ffprobe") or "ffprobe"
    if not ffmpeg:
        print("test_make_placeholder_clip SKIP (sin ffmpeg)")
        return
    tmp = Path(__file__).parent / "_tmp_placeholder"
    try:
        dst = tmp / "placeholder.mp4"
        m.make_placeholder_clip(ffmpeg, dst, 2, "Escena 1")
        assert dst.exists() and dst.stat().st_size > 0
        info = m.probe_video(ffprobe, dst)
        assert info["duration"] and abs(info["duration"] - 2) < 0.3, info
        assert info["has_audio"], info
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    print("test_make_placeholder_clip OK")


def test_normalize_aspect():
    assert m.normalize_aspect("h") == "16:9"
    assert m.normalize_aspect("horizontal") == "16:9"
    assert m.normalize_aspect("Horizontal (16:9)") == "16:9"
    assert m.normalize_aspect("v") == "9:16"
    assert m.normalize_aspect("vertical") == "9:16"
    assert m.normalize_aspect("c") == "1:1"
    assert m.normalize_aspect("cuadrado") == "1:1"
    assert m.normalize_aspect("16:9") == "16:9"
    assert m.normalize_aspect("21:9") == "21:9"
    assert m.normalize_aspect("") == "16:9"
    assert m.normalize_aspect(None) == "16:9"
    assert m.normalize_aspect("panorámico") == "16:9"  # no reconocido -> 16:9
    print("test_normalize_aspect OK")


def test_srt_helpers():
    import shutil
    assert m.format_srt_ts(0.3) == "00:00:00,300"
    assert m.format_srt_ts(61.5) == "00:01:01,500"
    assert m.format_srt_ts(3661.25) == "01:01:01,250"
    assert m.wrap_text("hola mundo", width=6) == "hola\nmundo"
    tmp = Path(__file__).parent / "_tmp_srt"
    try:
        p = tmp / "clip_01.srt"
        m.build_scene_srt("Así comienza la aventura espacial.", 5.0, 5.0, p)
        content = p.read_text(encoding="utf-8")
        assert "00:00:00,150" in content
        # karaoke: una pista por palabra, con la palabra resaltada en su pista
        cues = [ln for ln in content.splitlines() if "-->" in ln]
        assert len(cues) == 5, cues  # 5 palabras
        assert content.count("<b>") == 5
        assert f'<font color="{m.SUBTITLE_HIGHLIGHT}"><b>comienza</b></font>' in content
        # el tiempo total de las pistas cubre la duración del audio
        last_end = cues[-1].split("-->")[1].strip().split()[0]
        assert last_end >= m.format_srt_ts(4.9)
        comb = tmp / "subtitles.srt"
        m.build_combined_srt([("Escena uno", 0.0, 5.0), ("Escena dos", 5.0, 9.0)], comb)
        c2 = comb.read_text(encoding="utf-8")
        assert "00:00:05,000 --> 00:00:09,000" in c2, c2
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    print("test_srt_helpers OK")


def test_parse_script_scenes():
    # guion JSON con menos escenas que el mínimo por defecto: se acepta igual
    raw = json.dumps({"scenes": [{"prompt": f"P{i}", "narration": f"N{i}", "duration": 4}
                                  for i in range(1, 4)]})
    got = m.parse_script_scenes(raw)
    assert len(got) == 3 and got[0]["duration"] == 4
    # lista directa
    assert len(m.parse_script_scenes(json.dumps([{"prompt": "x"}, {"prompt": "y"}]))) == 2
    # sin duration -> None; narración opcional
    s = m.parse_script_scenes(json.dumps([{"prompt": "x"}]))[0]
    assert s["duration"] is None and s["narration"] is None
    # texto no JSON -> None
    assert m.parse_script_scenes("Escena 1: un robot despierta") is None
    # parse_scenes_json (para el LLM) sí exige el mínimo
    assert m.parse_scenes_json(raw) is None
    print("test_parse_script_scenes OK")


if __name__ == "__main__":
    test_parse_scenes_json()
    test_naive_storyboard()
    test_target_dims()
    test_normalize_aspect()
    test_extract_video_url()
    test_resolve_url()
    test_resolve_duration_fallback()
    test_resolve_duration_round_up_and_clip_duration()
    test_is_restriction_error()
    test_make_placeholder_clip()
    test_srt_helpers()
    test_parse_script_scenes()
    print("TODO OK")
