# TresClips 🎬 — Clips con guion temporal, narración en español latinoamericano y montaje FFmpeg

Aplicación que, **partiendo de una sola idea** del usuario:

1. **Storyboard con guion temporal**: un LLM de OpenRouter (por defecto `deepseek/deepseek-v4-flash-0731`) convierte la idea en **6 escenas consecutivas**, cada una con prompt de vídeo, narración en español (latinoamericano) y una **duración sugerida**.
2. **Generación con duración variable**: la API de vídeo de **OpenRouter** (por defecto `bytedance/seedance-2.0-mini`) genera cada clip con la duración que necesita su escena. Con narración activa, la duración se **ajusta a la duración real de la narración** (medida con FFmpeg), redondeada a los valores que soporta el modelo.
3. **Narración**: la API de **texto-a-voz (TTS)** de OpenRouter (por defecto `deepgram/aura-2`, voz **`aura-2-alvaro-es`**, masculina, español latinoamericano) genera la voz en off de cada escena.
4. **Montaje**: **FFmpeg** mezcla cada clip con su narración, normaliza todo (mismo códec H.264, resolución y fps) y **concatena el MP4 final**.

```
  Tu idea ──▶ OpenRouter (storyboard: 6 escenas con timecodes)
        ──▶ OpenRouter (clips de duración variable) ──▶ OpenRouter TTS (narración es-LatAm)
        ──▶ FFmpeg (mezcla + MP4 final)
```

---

## Requisitos

- **Python 3.10+**: `pip install -r requirements.txt` (solo usa `requests`).
- **FFmpeg** en el PATH (`ffmpeg -version`). En Windows: [gyan.dev builds](https://www.gyan.dev/ffmpeg/builds/) o `winget install ffmpeg`.
- **Clave de API de OpenRouter** y **saldo/créditos** en la cuenta: <https://openrouter.ai/settings/keys>

## Instalación y configuración

```bash
pip install -r requirements.txt
copy .env.example .env      # en Windows (o: cp .env.example .env en Linux/Mac)
```

Edita `.env` y pega tu clave:

```
OPENROUTER_API_KEY=tu_clave_aqui
```

> 💰 OpenRouter funciona con **pago por uso** (prepago). El modelo por defecto
> (`bytedance/seedance-2.0-mini`) es de los más baratos del catálogo; el TTS
> `deepgram/aura-2` cobra por carácter (muy poco). Consulta los precios vigentes
> en <https://openrouter.ai/models>.

## Uso

```bash
# 6 clips de duración variable ajustada a su narración (masculina, es-LatAm) → out/final_video.mp4
python main.py "Un robot explorador descubre una ciudad submarina olvidada"

# Con tu propio guion: JSON de escenas (prompt/narration/duration) o guion en texto libre
python main.py --script mi_guion.json
python main.py --script "Escena 1: un robot despierta... Escena 2: explora..."

# Si el guion JSON trae N escenas, se usan N clips automáticamente (--clips se ignora)

# Sin narración (clips fijos de --duration segundos, en silencio o con su audio)
python main.py "Un robot explorador descubre una ciudad submarina olvidada" --no-narration

# Con narración pero duración fija (--duration) en todos los clips
python main.py "Mi idea" --fixed-duration --duration 5

# Más clips / otro ritmo
python main.py "Mi idea" --clips 8

# Sin argumentos: te pide la idea (o 'guion: …')
python main.py
```

## Interfaz web (Streamlit)

La misma aplicación tiene una **interfaz web** con Streamlit:

```bash
streamlit run app.py
```

Se abre en el navegador (normalmente `http://localhost:8501`) y permite:

- Escribir la **idea** o pegar tu **guion** (selector "¿Qué quieres pegar?"), subir un
  **logo** (esquina superior izquierda del primer clip) y/o **música de fondo** (con su
  volumen), y lanzar **"🎬 Generar vídeo"** o **"🧪 Modo demo (sin API)"**.
- Configurar desde la barra lateral: nº de clips, duración base, modelo de vídeo,
  modelo TTS y **voz** (por defecto `aura-2-alvaro-es`, masculina, español
  latinoamericano), **proporciones (horizontal / vertical / cuadrado)**, resolución,
  FPS y opciones (sin narración, duración fija, audio del modelo, alargar, sin
  storyboard…).
- Escribir la **clave de OpenRouter** directamente en la interfaz (si se deja
  vacía, se usa la del archivo `.env`).
- **"📋 Ver modelos de vídeo"** para listar el catálogo disponible.
- Seguir el pipeline **en vivo**: registro (log), barra de progreso por escena,
  y al terminar el **guion con indicaciones temporales**, el **reproductor de
  vídeo** y el botón de **descarga del MP4**.

`app.py` usa `main.py` como motor (lo ejecuta en un hilo y captura su salida),
así que el comportamiento es idéntico al de la línea de comandos.

## Despliegue en Streamlit Cloud (clave API por usuario)

La app está preparada para **Streamlit Community Cloud sin secretos**: **cada
usuario pone su propia clave de OpenRouter** en la barra lateral (se usa solo en
su sesión, no se guarda ni se comparte). No hay que configurar ninguna clave en
Streamlit.

**Alternativa (recomendada para un despliegue privado):** configura la clave en
los **secretos de Streamlit**:

- En la nube: **Streamlit Cloud → Settings → Secrets** →
  `OPENROUTER_API_KEY = "sk-or-..."`.
- En local: copia `.streamlit/secrets.example.toml` a `.streamlit/secrets.toml`
  y pega tu clave (ese archivo está en `.gitignore`, no se sube).

Con secretos configurados, los usuarios **no necesitan escribir la clave** (la
app la toma de `st.secrets`); si un usuario escribe la suya en la barra lateral,
esa prevalece solo para su sesión.

Pasos:

1. Sube el proyecto a un repositorio de **GitHub** (no subas `.env`: `.gitignore`
   ya lo excluye).
2. Entra en <https://share.streamlit.io> → **Create app** → conecta el repositorio
   y elige `app.py`.
3. En **Advanced settings** → **Python version**: elige **3.11** (o superior).
4. Despliega. Los archivos de soporte hacen el resto:
   - `packages.txt` → instala **ffmpeg** (y ffprobe) en el contenedor;
   - `.streamlit/config.toml` → tema y `headless`;
   - `requirements.txt` → `requests` + `streamlit`.

Notas sobre la nube:

- **Aislamiento por sesión**: cada sesión usa su propia carpeta temporal (los
  contenedores son compartidos entre usuarios) y su propia clave, pasada al
  pipeline por parámetro (sin variables globales compartidas).
- **Prueba sin gastar**: el botón **"🧪 Modo demo (sin API)"** funciona sin clave.
- **Limitaciones del plan gratis**: la generación tarda varios minutos y conviene
  dejar la pestaña abierta; los archivos viven en el contenedor (efímero), así que
  **descarga el MP4** al terminar.

Al final de la generación, la app imprime el **guion con indicaciones temporales**,
p. ej.:

```
📋 Guion con indicaciones temporales:
     Escena  1  00:00 – 00:05  (5 s)  🎙 Así comienza la aventura espacial.
     Escena  2  00:05 – 00:09  (4 s)  🎙 El robot mira la marca de la nave.
     Escena  3  00:09 – 00:13  (4 s)  🎙 La Tierra brilla a lo lejos.
     …
```

### Opciones principales

| Opción | Descripción | Por defecto |
|---|---|---|
| `--script` | **Guion propio**: ruta de archivo o texto. JSON con `{"scenes": […]}` (se usa tal cual) o guion libre (se convierte con el LLM) | — |
| `--clips N` | Número de clips/escenas | `6` |
| `--duration N` | Duración base. Con narración activa, cada clip se ajusta a la duración real de su narración (variable) | `5` |
| `--fixed-duration` | Con narración activa, usar siempre `--duration` en vez de ajustar cada clip | off |
| `--model MODELO` | Modelo de vídeo de OpenRouter | `bytedance/seedance-2.0-mini` |
| `--llm-model MODELO` | LLM para el storyboard (`auto` elige uno) | `auto` |
| `--tts-model MODELO` | Modelo de texto-a-voz (TTS) | `deepgram/aura-2` |
| `--voice VOZ` | Voz TTS (masculina, español latinoamericano) | `aura-2-alvaro-es` |
| `--no-narration` | No generar narración TTS | off |
| `--no-subtitles` | No quemar subtítulos (por defecto se queman los de la narración y se genera `subtitles.srt`) | off |
| `--logo RUTA` | Imagen de logo (png/jpg…) que se superpone pequeña en la esquina superior izquierda del **primer clip** | — |
| `--music RUTA` | Música de fondo (mp3/wav…) mezclada a bajo volumen bajo la narración en todos los clips | — |
| `--music-volume 0-1` | Volumen de la música de fondo | `0.2` |
| `--base-url URL` | URL base de la API de OpenRouter | `https://openrouter.ai/api/v1` |
| `--aspect-ratio` | Proporciones: **horizontal**, **vertical**, **cuadrado** (o 16:9, 9:16, 1:1…). En la CLI se **pregunta** si no se indica | pregunta en CLI / selector en Streamlit |
| `--resolution` | `480p`, `720p`, `1080p`, `2K`, `4K` o `auto` | `1080p` |
| `--audio` | Pide audio generado junto al vídeo; con narración activa, la narración lo sustituye | off |
| `--retries N` | Intentos por clip/narración ante rechazos por política/restricciones | `3` |
| `--no-placeholder` | Si un clip se rechaza por restricciones, omitirlo en vez de crear un clip de reserva | off |
| `--fps N` | Fotogramas por segundo del montaje | `24` |
| `--out-dir` | Carpeta de salida | `out` |
| `--final-name` | Nombre del MP4 final | `final_video.mp4` |
| `--lengthen` | Alarga en cámara lenta los clips que no lleguen a `--duration` | off |
| `--no-storyboard` | No usa LLM para el guion (plantillas simples) | off |
| `--demo` | Prueba el pipeline FFmpeg con clips sintéticos (sin API) | off |
| `--list-models` | Lista los modelos de vídeo y sus capacidades | — |

### Modelos de vídeo (OpenRouter)

La app consulta `GET {base}/videos/models` y **ajusta automáticamente duración,
resolución y aspecto** a lo que soporta el modelo elegido. Ejemplos actuales:

| Modelo | Duraciones | Resoluciones | Audio |
|---|---|---|---|
| `bytedance/seedance-2.0-mini` *(por defecto)* | **4–15 s** (¡5 s nativos!) | 480p, 720p | sí |
| `google/veo-3.1` | 4, 6, 8 s | 720p, 1080p, 4K | sí |
| `google/veo-3.1-fast` | 4, 6, 8 s | 720p, 1080p, 4K | sí |
| `google/veo-3.1-lite` (más barato) | 4, 6, 8 s | 720p, 1080p | sí |

Con el modelo por defecto (Seedance 2.0 Mini), los **6 clips de 5 s** se generan
directamente. Los modelos Veo solo llegan a **8 s**: si pides más, la app usa 8 s
y te avisa; añade `--lengthen` para alargarlos en cámara lenta con FFmpeg.
Para ver el catálogo completo (Sora, Kling, Seedance, Hailuo…): `python main.py --list-models`.

## Cómo funciona la integración con OpenRouter

- **Storyboard**: `POST {base}/chat/completions` pidiendo un JSON con N escenas
  (`prompt` + `narration` en español + `duration` sugerida), con
  `response_format: json_object` y reintento si el modelo no lo acepta.
- **Vídeo** (API oficial de vídeo de OpenRouter):
  1. `POST {base}/videos` con `{model, prompt, duration, aspect_ratio, resolution?,
     generate_audio?}` → devuelve `{id, status, polling_url}`.
  2. Se consulta la `polling_url` cada `--poll-interval` segundos hasta `completed`
     (estados terminales: `failed`, `cancelled`, `expired`).
  3. Al completar, se descarga el MP4 de `unsigned_urls[0]` (con el token solo si la
     URL pertenece a la propia API); si no hay URL, se usa
     `{base}/videos/{id}/content?index=0`.
  - Si la API rechaza un parámetro opcional (400), se reintenta sin él automáticamente.
- **Duración variable**: con narración activa, la app primero genera el TTS, mide la
  duración real del audio con `ffprobe` y pide al modelo de vídeo un clip de
  `ceil(narración + 0.75 s)` redondeado a la duración soportada más cercana
  (Seedance Mini: enteros de 4 a 15 s; Veo: 4/6/8 s). Si no hay narración, usa la
  duración sugerida por el guion o `--duration`.
- **Narración (TTS)**: `POST {base}/audio/speech` (compatible con OpenAI) con
  `{model, input, voice, response_format: "mp3"}` → audio crudo. Por defecto
  `deepgram/aura-2` con la voz **`aura-2-alvaro-es`** (masculina, español
  latinoamericano). FFmpeg mezcla cada narración con su clip (la narración
  sustituye al audio del clip y se ajusta a la duración exacta del clip).
- **Subtítulos**: con narración activa (y salvo `--no-subtitles`), el texto de
  cada narración se **quema en el vídeo** con **estilo karaoke**: letra pequeña
  (fija en el código) que **resalta en amarillo la palabra que se está leyendo**,
  con sincronización palabra por palabra a partir de la duración real del audio.
  Además se genera **`subtitles.srt`** (una pista por escena, con timecodes
  acumulados) descargable desde la interfaz.
- **Música de fondo**: con `--music RUTA` (o subiendo el archivo en la interfaz),
  la música se mezcla a bajo volumen (`--music-volume`, por defecto 0.2) bajo la
  narración en todos los clips (filtro `amix` de FFmpeg).

## Pruebas sin gastar créditos

```bash
python main.py --demo                                # pipeline FFmpeg completo (sin API)
python test_mock_server.py                           # pipeline completo contra un servidor simulado de OpenRouter
python test_mock_server.py --no-rephrase             # variante: restricción que no se puede reescribir -> clip de reserva
python test_units.py                                 # tests unitarios
python test_app.py                                   # motor en proceso + render de la app Streamlit (AppTest)
```

`test_mock_server.py` levanta un servidor local que imita `chat/completions`,
`videos` (con polling), el **TTS** (`/audio/speech`), la descarga de MP4 y
restricciones de política (de vídeo y de narración) en la escena 2, y ejecuta
`main.py` de principio a fin sin clave real.

## Salida

```
out/
├── clips/               # los 6 clips generados (clip_01.mp4, …)
├── narration/           # audios de narración TTS por escena (narration_01.mp3, …)
├── subtitles/           # subtítulos por escena (clip_01.srt, …)
├── clips_norm/          # versiones normalizadas con narración y subtítulos
├── subtitles.srt        # subtítulos del vídeo completo (con timecodes)
└── final_video.mp4      # 🎉 el vídeo final (clips + narración + subtítulos)
```

## Restricciones legales / política de contenido

Si OpenRouter o el proveedor **rechaza un clip o la narración (audio)** por una
restricción legal o de política de contenido (copyright, personajes/marcas
protegidos, filtros RAI, contenido no permitido…), la app **no interrumpe la
generación del vídeo**:

1. Detecta el rechazo (mensajes con *policy, copyright, safety, RAI, restricted…*).
2. **Reescribe el prompt** (o la **narración**) pidiendo al LLM una versión que
   cumpla la política y **reintenta** — hasta `--retries` veces.
3. Si el clip llevaba `--audio` y la restricción persiste, **reintenta sin audio**.
4. Si se agotan las opciones para un **clip**, crea un **clip de reserva** (fondo
   con "Escena N") para que el MP4 final siempre se genere; si se agotan para una
   **narración**, ese clip va en silencio. Con `--no-placeholder` el clip se omite.

Solo los errores **no relacionados con restricciones** (p. ej. saldo insuficiente,
timeout, error 500) detienen la ejecución, para no ocultar problemas reales.

## Solución de problemas

- **`No se encontró OPENROUTER_API_KEY`** → crea `.env` a partir de `.env.example` y pega tu clave.
- **HTTP 401** → clave inválida o sin saldo activo; revisa <https://openrouter.ai/settings/keys>.
- **HTTP 402** → **saldo insuficiente** en OpenRouter: recarga créditos.
- **HTTP 429** → límite de peticiones: espera o sube `--poll-interval`.
- **HTTP 400** → la app reintenta automáticamente sin parámetros opcionales; si persiste,
  prueba `--resolution auto`.
- **`model not found`** → el slug del modelo cambia con el tiempo: ejecuta `python main.py --list-models` y usa ese id con `--model`.
- **Duración menor de lo pedido** → Veo genera como máximo 8 s; usa `--lengthen` para llegar a 10 s, o elige otro modelo del catálogo.
- **Marca de agua** → es normal: OpenRouter/Google aplican SynthID a los vídeos generados.
- **`ffmpeg` no encontrado** → instálalo o usa `--ffmpeg C:\ruta\a\ffmpeg.exe`.

## Notas

- Cada clip puede tardar **varios minutos** en generarse (cola del modelo). Hay un timeout de 15 min por clip (`--timeout`).
- OpenRouter puede aplicar un margen sobre el precio del proveedor; revisa los precios antes de lanzar muchas generaciones.
- Este proyecto es una demo educativa: usa la API oficial de OpenRouter con tu propia clave y saldo.
