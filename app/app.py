#!/usr/bin/env python3
"""text2CAD Web-Service — Text-zu-CAD mit OpenRouter / OmniRouter.

Wrapper um https://github.com/earthtojake/text-to-cad (cadgen/build123d-Skills):
- Prompt -> LLM (OpenAI-kompatibel) -> build123d-Python -> cadgen-Build -> STEP/STL/3MF
- Fortschrittsbalken via Polling (/api/jobs/{id}), Voll-Logs (stderr/stdout-Chain)
- 3D-Vorschau im Browser via Three.js STLLoader (nicht live, nach Fertigstellung)
- 3D-Druck-optimiert: STL/3MF-Export + DfAM-Basischecks (watertight, Volumen, BBox)

Bindet auf 0.0.0.0 (LXC), Port via ENV PORT (Default 8080).
Keine Cloud-Pflicht ausser dem gewaehlten LLM-Gateway (Key optional, lokale
Fallback-Beispiele ohne Key).
"""
from __future__ import annotations

import ast
import asyncio
import json
import os
import re
import shutil
import subprocess
import sys
import time
import traceback
import uuid
from datetime import datetime
from pathlib import Path

import httpx
from fastapi import BackgroundTasks, FastAPI, HTTPException
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

# ---------------------------------------------------------------- Config
PORT = int(os.environ.get("PORT", "8080"))
DATA_DIR = Path(os.environ.get("DATA_DIR", "/opt/text2cad/data"))
JOBS_DIR = DATA_DIR / "jobs"
SETTINGS_FILE = DATA_DIR / "settings.json"
STATIC_DIR = Path(__file__).parent / "static"

DATA_DIR.mkdir(parents=True, exist_ok=True)
JOBS_DIR.mkdir(parents=True, exist_ok=True)

OPENROUTER_BASE_DEFAULT = "https://openrouter.ai/api/v1"
OMNIROUTER_BASE_DEFAULT = os.environ.get("OMNIROUTER_BASE_URL", "http://localhost:20128/v1")

# Kuratierte Fallback-Modelle (wenn kein Key / Gateway offline).
FALLBACK_MODELS = {
    "openrouter": [
        {"id": "anthropic/claude-sonnet-4", "name": "Claude Sonnet 4 (OpenRouter)"},
        {"id": "openai/gpt-4o", "name": "GPT-4o (OpenRouter)"},
        {"id": "google/gemini-2.5-pro", "name": "Gemini 2.5 Pro (OpenRouter)"},
        {"id": "deepseek/deepseek-chat", "name": "DeepSeek Chat (OpenRouter)"},
        {"id": "qwen/qwen3-coder:free", "name": "Qwen3 Coder :free (OpenRouter)"},
        {"id": "meta-llama/llama-3.3-70b-instruct", "name": "Llama 3.3 70B (OpenRouter)"},
    ],
    "omnirouter": [
        {"id": "gpt-4o", "name": "GPT-4o (OmniRouter)"},
        {"id": "claude-sonnet-4", "name": "Claude Sonnet 4 (OmniRouter)"},
        {"id": "llama-3.3-70b-versatile", "name": "Llama 3.3 70B (OmniRouter)"},
        {"id": "qwen3-coder", "name": "Qwen3 Coder (OmniRouter)"},
    ],
    "custom": [],
}

SYSTEM_PROMPT = """Du bist ein CAD-Code-Generator für build123d + cadgen (text-to-cad Skill).
REGELN (strikt einhalten):
- Gib GENAU EINEN Python-Codeblock mit ```python ... ``` zurück, sonst nichts davor/danach ausser einer Zeile Zusammenfassung.
- Einheiten: Millimeter. Ursprung: Zentrum des Hauptteils, Basis XY, Extrusion +Z.
- Nutze: from cadgen import build123d as bd / from cadgen import step, stl, threemf
- EXAKT EINE Modellfunktion ohne Parameter mit Decoratoren @step UND @stl, z.B.:
```python
from cadgen import build123d as bd
from cadgen import step, stl

W = 20.0

@step
@stl
def part():
    return bd.Box(W, 10, 5)

if __name__ == "__main__":
    part()
```
- Optional zusätzlich @threemf wenn 3MF gewünscht (einfach stapeln).
- Rückgabe ist ein geschlossener Solid (Box, Cylinder, extrusions, fillet/chamfer wo sinnvoll).
- 3D-DRUCK (FDM): Wandstaerke >= 1.2mm (besser 2mm), keine freischwebenden Strukturen ohne Fase, Ueberhaenge >=45 Grad vermeiden bzw. anfasen, manifold/geschlossen.
- Benutze benannte Konstanten (WIDTH, HEIGHT...), verbose Labels wo möglich.
- KEINE Imports ausser cadgen/build123d/math. KEIN os/sys/subprocess/socket/open/network. KEIN Lesen/Schreiben von Dateien.
- Halte das Teil einfach und robust: lieber primitives + Bohrungen/Fasen als komplexe Lofts.
- Transformationen NUR nach build123d-0.11-Doku (https://build123d.readthedocs.io/en/latest/moving_objects.html):
  Rotation als `teil.rotate(bd.Axis((0,0,0),(1,0,0)), 15)` (Axis-Objekt aus Startpunkt+Richtung, Winkel in GRAD, positional)
  oder `bd.Rotation(15,0,0) * teil` (Euler-X/Y/Z in Grad). Translation als `teil.translate((x,y,z))`
  oder `bd.Pos(x,y,z) * teil`. VERBOTEN: rotate/move mit rohem Tupel als Achse, Keyword `angle=`.
- Kanten/Faces NUR so: Fasen/Verrundungen als Modul-Funktionen `bd.chamfer(teil.edges().filter_by(...), 0.5)`
  bzw. `bd.fillet(...)`. VERBOTEN: String-Selektoren wie faces(">Z")/edges("|Z") (gibt es in 0.11 nicht —
  faces()/edges() nehmen keine Positionsargumente), .chamfer()/.fillet() als Methoden.
"""

MAX_BUILD_REPAIR = 2  # auto. Reparaturversuche mit Fehler-Feedback nach einem Fehlschlag (1 Erstversuch + 2 Repairs)

CODE_BLOCK_RE = re.compile(r"```python\s*(.*?)```", re.DOTALL | re.IGNORECASE)
FORBIDDEN_RE = re.compile(
    r"\b(os\.system|subprocess|socket|urllib|requests\.|open\s*\(|__import__|eval\s*\(|exec\s*\()",
    re.IGNORECASE,
)

app = FastAPI(title="text2CAD", version="1.0.0")

# ---------------------------------------------------------------- Models
class Settings(BaseModel):
    openrouter_key: str = ""
    omnirouter_url: str = OMNIROUTER_BASE_DEFAULT
    omnirouter_key: str = ""
    custom_url: str = ""
    custom_key: str = ""
    default_provider: str = "openrouter"
    default_model: str = ""
    default_exports: list[str] = ["stl", "step"]
    default_snapshot: bool = True


class GenerateRequest(BaseModel):
    prompt: str = Field(min_length=3, max_length=4000)
    provider: str = Field(default="openrouter", pattern="^(openrouter|omnirouter|custom)$")
    model: str = Field(min_length=1, max_length=200)
    api_key: str = ""
    base_url: str = ""
    exports: list[str] = ["stl", "step"]  # subset von stl/step/3mf
    want_snapshot: bool = True
    parent_job: str = ""  # gesetzt = Änderungsmodus: model.py dieses Jobs als Basis


# In-Memory Jobstore (wird zusaetzlich nach jobs/{id}/job.json persistiert)
JOBS: dict[str, dict] = {}


def load_settings() -> dict:
    if SETTINGS_FILE.exists():
        try:
            return json.loads(SETTINGS_FILE.read_text())
        except Exception:
            return {}
    return {}


def save_settings(data: dict) -> None:
    SETTINGS_FILE.write_text(json.dumps(data, indent=2))


def job_dir(job_id: str) -> Path:
    d = JOBS_DIR / job_id
    d.mkdir(parents=True, exist_ok=True)
    return d


def persist_job(job: dict) -> None:
    try:
        (job_dir(job["id"]) / "job.json").write_text(json.dumps(job, indent=2, default=str))
    except Exception:
        pass


def log(job: dict, msg: str) -> None:
    line = f"[{datetime.now().strftime('%H:%M:%S')}] {msg}"
    job.setdefault("logs", []).append(line)
    print(line, flush=True)
    persist_job(job)


def set_progress(job: dict, pct: int, stage: str) -> None:
    job["progress"] = max(0, min(100, pct))
    job["stage"] = stage
    log(job, f"{pct:3d}% — {stage}")
    persist_job(job)


def resolve_gateway(req: GenerateRequest, settings: dict) -> tuple[str, str]:
    """-> (base_url, api_key). Leere api_key = anonymer Versuch / Fallback."""
    if req.provider == "openrouter":
        base = (req.base_url or settings.get("openrouter_url") or OPENROUTER_BASE_DEFAULT).rstrip("/")
        key = req.api_key or settings.get("openrouter_key", "") or os.environ.get("OPENROUTER_API_KEY", "")
        return base or OPENROUTER_BASE_DEFAULT, key
    if req.provider == "omnirouter":
        base = (req.base_url or settings.get("omnirouter_url") or OMNIROUTER_BASE_DEFAULT).rstrip("/")
        key = req.api_key or settings.get("omnirouter_key", "") or os.environ.get("OMNIROUTER_API_KEY", "")
        return base, key
    # custom: OpenAI-kompatibel, URL aus Request oder gespeicherten Einstellungen
    base = (req.base_url or settings.get("custom_url") or "").rstrip("/")
    if not base:
        raise HTTPException(400, "Für Provider 'custom' wird base_url benötigt — auf der Einstellungsseite (/settings) hinterlegen oder pro Request mitsenden (OpenAI-kompatibel, z.B. http://host:11434/v1).")
    key = req.api_key or settings.get("custom_key", "") or ""
    return base, key


def extract_code(text: str) -> str:
    m = CODE_BLOCK_RE.search(text or "")
    if m:
        return m.group(1).strip()
    # Fallback: ganzer Text wenn er nach Python aussieht
    t = (text or "").strip()
    if "def " in t and ("build123d" in t or "cadgen" in t):
        return t
    raise ValueError("LLM-Antwort enthielt keinen ```python-Codeblock mit cadgen-Modell.")


def sanitize_code(code: str) -> str:
    if FORBIDDEN_RE.search(code):
        raise ValueError("Generierter Code nutzt verbotene APIs (os/subprocess/socket/open/eval) — verworfen.")
    if "build123d" not in code and "cadgen" not in code:
        raise ValueError("Code nutzt weder build123d noch cadgen — verworfen (vermutlich Prosa statt Code).")
    if "@step" not in code and "@stl" not in code:
        raise ValueError("Code enthaelt keinen @step/@stl Decorator — verworfen.")
    try:
        ast.parse(code)
    except SyntaxError as e:
        raise ValueError(f"Kein gültiges Python (SyntaxError Zeile {e.lineno}: {e.msg}) — verworfen.") from e
    if "__main__" not in code:
        code += '\n\nif __name__ == "__main__":\n    part()\n'
    return code


def ensure_exports(code: str, exports: list[str]) -> str:
    """Stellt sicher, dass gewünschte Decoratoren vorhanden sind."""
    wants_3mf = "3mf" in [e.lower() for e in exports]
    if wants_3mf and "threemf" not in code:
        code = code.replace("from cadgen import step, stl", "from cadgen import step, stl, threemf")
        if "from cadgen import step, stl, threemf" not in code and "from cadgen import" in code:
            # generisch ergänzen
            code = code.replace("from cadgen import", "from cadgen import threemf,", 1) if "threemf" not in code else code
        code = code.replace("@step\n@stl", "@step\n@stl\n@threemf").replace("@stl\n@step", "@stl\n@threemf\n@step")
    return code


def _message_text(msg: dict) -> str:
    """Antworttext aus Chat-Completion-Message extrahieren.

    Manche Modelle (v.a. :free-/Reasoning-Modelle auf OpenRouter) liefern
    content=null und legen den Text in reasoning-Feldern ab; andere liefern
    Content-Block-Listen (Anthropic-Stil). Alles abdecken, sonst "".
    """
    content = msg.get("content")
    if isinstance(content, list):
        parts = []
        for b in content:
            if isinstance(b, dict) and b.get("type") in ("text", "output_text", "reasoning_text"):
                parts.append(str(b.get("text", "")))
            elif isinstance(b, str):
                parts.append(b)
        content = "\n".join(p for p in parts if p)
    if isinstance(content, str) and content.strip():
        return content
    for key in ("reasoning", "reasoning_content", "thinking"):
        val = msg.get(key)
        if isinstance(val, str) and val.strip():
            return val
    return ""


async def llm_generate(base_url: str, api_key: str, model: str, user_content: str, job: dict) -> str:
    """user_content ist der fertige User-Prompt (Neu-Erstellung oder Änderungswunsch inkl. Basis-Code)."""
    url = base_url.rstrip("/") + "/chat/completions"
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    # OpenRouter-Empfehlungen (harmlos für andere Gateways)
    headers.setdefault("HTTP-Referer", "http://localhost:8080/")
    headers.setdefault("X-Title", "text2CAD-local")
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user_content},
        ],
        "temperature": 0.2,
        "max_tokens": 2500,
    }
    log(job, f"LLM-Request: POST {url} model={model}")
    try:
        async with httpx.AsyncClient(timeout=120) as c:
            r = await c.post(url, headers=headers, json=payload)
    except Exception as e:
        raise RuntimeError(f"Gateway nicht erreichbar ({url}): {e}\n{traceback.format_exc()}") from e
    if r.status_code >= 400:
        raise RuntimeError(f"LLM-Fehler HTTP {r.status_code} bei {url} model={model}:\n{r.text[:4000]}")
    try:
        data = r.json()
        text = _message_text(data["choices"][0]["message"])
    except Exception as e:
        raise RuntimeError(f"Unerwartete LLM-Antwort (kein choices[0].message):\n{r.text[:4000]}\nFehler: {e}") from e
    if not (text or "").strip():
        raise RuntimeError(
            "LLM lieferte leeren Content (content=null — typisch bei Reasoning-/Free-Modellen).\n"
            f"Volle Antwort:\n{json.dumps(data, indent=2)[:4000]}\n"
            "Tipp: per Dropdown ein anderes Modell wählen (kein :free-Reasoning-Modell)."
        )
    return text


def run_cmd(cmd: list[str], cwd: Path, timeout: int = 180) -> tuple[int, str, str]:
    p = subprocess.run(cmd, cwd=str(cwd), capture_output=True, text=True, timeout=timeout)
    return p.returncode, p.stdout, p.stderr


def run_build(d: Path, timeout: int = 240) -> tuple[int, str, str]:
    """Ein CAD-Build (python model.py). rc 124 = Timeout (kein Repair sinnvoll)."""
    # Umgebungs-Blindheit von cadgen beachten: nur Dateien als Input
    env = dict(os.environ)
    env["CADGEN_DAEMON"] = "0"
    try:
        p = subprocess.run(
            [sys.executable, "model.py", "--force"],
            cwd=str(d), capture_output=True, text=True, timeout=timeout, env=env,
        )
        return p.returncode, p.stdout or "", p.stderr or ""
    except subprocess.TimeoutExpired as e:
        out = e.stdout if isinstance(e.stdout, str) else ""
        err = (e.stderr if isinstance(e.stderr, str) else "") + f"\n[TIMEOUT nach {timeout}s]"
        return 124, out or "", err


def env_error_hint(err: str) -> str:
    """Erkennt Umgebungsfehler (kein Code-Problem) — dafür ist LLM-Repair sinnlos, Fail-fast mit Aktion."""
    e = err or ""
    if "NodeUnavailable" in e or "node was not found on PATH" in e or "CADGEN_NODE" in e:
        return ("UMGEBUNGSFEHLER (kein LLM-Repair versucht): cadgen braucht Node.js ≥20 für Mesh-Exporte (STL/3MF). "
                "Loesung: Installer-Einzeiler erneut laufen lassen — er installiert Node automatisch, danach Job neu starten.")
    if "ModuleNotFoundError" in e or "ImportError" in e:
        return ("UMGEBUNGSFEHLER (kein LLM-Repair versucht): Python-Paket fehlt im Container-venv. "
                "Loesung: Installer-Einzeiler erneut laufen lassen (venv wird neu gebaut), danach Job neu starten.")
    if "playwright" in e.lower() and "executable" in e.lower():
        return ("UMGEBUNGSFEHLER: Playwright-Browser fehlt — nur der PNG-Snapshot betroffen, STL-Download geht trotzdem. "
                "Loesung: Installer erneut laufen lassen.")
    return ""


def build_repair_prompt(code: str, err: str) -> str:
    tail = (err or "")[-3500:]
    return (
        "Der CAD-Code ist beim Bauen mit `python model.py` FEHLGESCHLAGEN. Analysiere die Fehlerausgabe, "
        "repariere die Ursache und gib das KOMPLETTE reparierte Modell als EINEN ```python-Block zurück "
        "(gleiche Decoratoren @step/@stl, mm, FDM-Regeln, nur cadgen/build123d-Imports).\n"
        "Bekannte build123d-0.11-Fallen (Doku https://build123d.readthedocs.io/en/latest/moving_objects.html):\n"
        "- Rotation NUR als `teil.rotate(bd.Axis((0,0,0),(x,y,z)), WINKEL_GRAD)` (Axis-Objekt, Grad, positional) "
        "oder `bd.Rotation(rx,ry,rz) * teil`. NIEMALS Tupel als Achse, NIEMALS Keyword `angle=`.\n"
        "- Translation: `teil.translate((x,y,z))` oder `bd.Pos(x,y,z) * teil`.\n"
        "- Fasen/Verrundungen NUR als `bd.chamfer(teil.edges().filter_by(...), 0.5)` / `bd.fillet(...)` "
        "(Modul-Funktionen). VERBOTEN: faces(\">Z\")/edges(\"|Z\") und .chamfer()/.fillet()-Methoden.\n"
        f"FEHLERAUSGABE:\n{tail}\n"
        f"FEHLERHAFTER CODE:\n```python\n{code[:8000]}\n```"
    )


def dfam_check(stl_path: Path) -> dict:
    """Basis-DfAM (FDM): watertight, Volumen, BBox. Volle Analyse siehe text-to-cad Skill dfam-check."""
    report: dict = {"file": stl_path.name, "tool": "trimesh-basis"}
    try:
        import trimesh
        m = trimesh.load(str(stl_path), force="mesh")
        if not hasattr(m, "is_watertight"):
            return {"skipped": True, "reason": "kein Mesh (evtl. Szene) — STL prüfen"}
        bbox = m.bounds  # [[x0..],[x1..]]
        dims = [round(float(bbox[1][i] - bbox[0][i]), 2) for i in range(3)]
        report.update({
            "watertight": bool(m.is_watertight),
            "volume_mm3": round(float(m.volume), 2),
            "bbox_mm": dims,
            "faces": int(len(m.faces)),
            "min_dim_mm": min(dims) if dims else 0,
        })
        hints: list[str] = []
        if not m.is_watertight:
            hints.append("Mesh ist NICHT wasserdicht — Slicer wird flicken; ggf. Prompt präzisieren (geschlossener Solid).")
        if min(dims) < 1.2:
            hints.append(f"Kleinste BBox-Dimension {min(dims)} mm < 1.2 mm — für FDM zu dünn, Wandstärke erhöhen.")
        elif min(dims) < 2.0:
            hints.append(f"Kleinste Dimension {min(dims)} mm < 2.0 mm — grenzwertig für FDM, 2 mm+ empfohlen.")
        else:
            hints.append("Wandstärken-Plausibilität OK (BBox-Heuristik, kein Ersatz für volles DfAM).")
        hints.append("Empfohlen: 0.2 mm Layer, 3 Wände, 15–20 % Infill, Überhänge ≥45° vermeiden.")
        report["hints"] = hints
    except ImportError:
        report = {"skipped": True, "reason": "trimesh nicht installiert"}
    except Exception as e:
        report = {"error": f"{e}\n{traceback.format_exc()}"}
    return report


def build_user_prompt(prompt: str, parent_job: str, job: dict) -> str:
    """Neu-Erstellung oder Änderungsmodus (bestehende model.py als Basis, minimal ändern)."""
    parent = (parent_job or "").strip()
    if not parent:
        return f"Erzeuge ein druckbares 3D-Teil (mm) für: {prompt}\nHalte dich strikt an das Ausgabeformat."
    if ".." in parent or "/" in parent:
        raise RuntimeError(f"Ungültige parent_job-ID: {parent}")
    pm = JOBS_DIR / parent / "model.py"
    if not pm.exists():
        raise RuntimeError(
            f"Parent-Job {parent} hat keine model.py — evtl. Job-ID falsch oder der Job ist fehlgeschlagen.\n"
            f"Geprüfter Pfad: {pm}"
        )
    job["parent_job"] = parent
    base_code = pm.read_text()[:12000]
    log(job, f"Änderungsmodus: Basis ist model.py aus Job {parent} ({len(base_code)} Zeichen).")
    return (
        "Bestehender CAD-Code — ändere NUR den Wunsch unten, alles andere exakt beibehalten:\n"
        "```python\n" + base_code + "\n```\n"
        f"Änderungswunsch: {prompt}\n"
        "Gib das KOMPLETTE geänderte Modell als EINEN ```python-Block zurück (gleiche Decoratoren, mm, FDM-Regeln)."
    )


async def run_job(job_id: str, req_data: dict) -> None:
    job = JOBS[job_id]
    settings = load_settings()
    d = job_dir(job_id)
    try:
        job["status"] = "running"
        set_progress(job, 4, "Starte: Gateway auflösen")
        base, key = resolve_gateway(GenerateRequest(**req_data), settings)
        job["base_url"] = base
        if not key:
            log(job, "WARNUNG: kein API-Key gesetzt — versuche anonymer Call; bei 401 bitte Key in Einstellungen hinterlegen.")

        set_progress(job, 10, f"Frage LLM ({req_data['model']}) …")
        user_content = build_user_prompt(req_data["prompt"], req_data.get("parent_job", ""), job)
        raw = await llm_generate(base, key, req_data["model"], user_content, job)
        if not isinstance(raw, str) or not raw.strip():  # Gürtel+Hosenträger (llm_generate wirft i.d.R. schon)
            raise RuntimeError("LLM lieferte leere Antwort (None/leer) — siehe vorigen Log + anderes Modell wählen.")
        (d / "llm_raw.md").write_text(raw)
        log(job, f"LLM-Antwort erhalten ({len(raw)} Zeichen). Volltext in llm_raw.md gesichert.")
        job["llm_raw"] = raw[:6000]

        set_progress(job, 38, "Extrahiere + prüfe Python-Code")

        max_attempts = 1 + MAX_BUILD_REPAIR
        rc, out, err = 1, "", ""
        code = ""
        last_raw = raw
        for attempt in range(1, max_attempts + 1):
            try:
                code = sanitize_code(ensure_exports(extract_code(last_raw), req_data.get("exports", ["stl", "step"])))
            except Exception as e:
                rc, out, err = 1, "", (f"Code-Validierung fehlgeschlagen (Build nicht versucht):\n{e}\n"
                                       f"Antwort-Ausschnitt (erste 2000 Zeichen):\n{(last_raw or '')[:2000]}")
            else:
                (d / "model.py").write_text(code)
                job["code"] = code
                if attempt == 1:
                    log(job, "Code-Validierung OK (cadgen/build123d, @step/@stl, gültiges Python, keine verbotenen APIs).")
                set_progress(job, 52, f"Baue CAD (Versuch {attempt}/{max_attempts}) …")
                rc, out, err = run_build(d)
            (d / "build.stdout.log").write_text(out)
            (d / "build.stderr.log").write_text(err)
            job["build"] = {"rc": rc, "attempt": attempt, "stdout": out[-8000:], "stderr": err[-8000:]}
            log(job, f"Versuch {attempt}: rc={rc}\n--- stdout (tail) ---\n{out[-3000:]}\n--- stderr/Validierung (tail) ---\n{err[-3000:]}")
            if rc == 0:
                break
            if rc == 124:
                raise RuntimeError(f"CAD-Build Timeout (Versuch {attempt}).\nVOLLSTDOUT:\n{out}\nVOLLSTDERR:\n{err}")
            hint = env_error_hint(err)
            if hint:
                raise RuntimeError(f"{hint}\nVOLLSTDOUT:\n{out}\nVOLLSTDERR:\n{err}")
            if attempt >= max_attempts:
                break
            log(job, f"Versuch {attempt}/{max_attempts} fehlgeschlagen — frage LLM nach Reparatur (Fehler-Feedback) …")
            last_raw = await llm_generate(base, key, req_data["model"], build_repair_prompt(code or last_raw, err), job)
            if not isinstance(last_raw, str) or not last_raw.strip():
                raise RuntimeError("LLM lieferte bei der Reparatur eine leere Antwort.")
            (d / "llm_repair.md").write_text(last_raw)
            log(job, f"Reparatur-Antwort erhalten ({len(last_raw)} Zeichen) — neuer Versuch.")
        if rc != 0:
            raise RuntimeError(f"CAD-Build nach {max_attempts} Versuchen fehlgeschlagen (Exit {rc}).\nVOLLSTDOUT:\n{out}\nVOLLSTDERR:\n{err}\n"
                               "Tipp: Scheitert immer dasselbe Modell schon an der Code-Erzeugung, "
                               "per Dropdown ein anderes (leistungsstärkeres) Modell wählen.")

        set_progress(job, 72, "Suche Exporte (STEP/STL/3MF)")
        files: dict[str, str] = {}
        for ext in ("stl", "step", "stp", "3mf", "glb"):
            for f in sorted(d.glob(f"*.{ext}")):
                files[ext if ext != "stp" else "step"] = f.name
        # 3MF-Nachbau falls gewünscht aber nicht deklariert: aus STL konvertieren
        if "3mf" in [e.lower() for e in req_data.get("exports", [])] and "3mf" not in files and "stl" in files:
            try:
                import trimesh
                m = trimesh.load(str(d / files["stl"]), force="mesh")
                m.export(str(d / "part.3mf"))
                files["3mf"] = "part.3mf"
                log(job, "3MF aus STL konvertiert (trimesh).")
            except Exception as e:
                log(job, f"3MF-Konvertierung übersprungen: {e}")
        # Fallback-Namen normalisieren
        for want, fname in (("stl", "part.stl"), ("step", "part.step"), ("3mf", "part.3mf")):
            if want in [e.lower() for e in req_data.get("exports", [])] and want not in files:
                cand = d / fname
                if cand.exists():
                    files[want] = fname
        job["files"] = files
        log(job, f"Exporte gefunden: {files if files else 'KEINE — model.py hat nichts geschrieben'}")
        if not files:
            raise RuntimeError("Keine Exportdatei erzeugt. Prüfe build.stderr.log — evtl. Decorator fehlt oder Model ist stale.")

        set_progress(job, 82, "DfAM-Basischeck (3D-Druck)")
        if "stl" in files:
            job["dfam"] = dfam_check(d / files["stl"])
            log(job, f"DfAM: {json.dumps(job['dfam'], indent=2, default=str)[:3000]}")
        else:
            job["dfam"] = {"skipped": True, "reason": "kein STL vorhanden"}
            log(job, "DfAM übersprungen (kein STL).")

        if req_data.get("want_snapshot", True):
            set_progress(job, 90, "Snapshot / Vorschau rendern")
            png = None
            for target, door in (("stl", "stl"), ("step", "step")):
                if target in files:
                    try:
                        rc, out, err = run_cmd(
                            ["cadgen", door, "snapshot", files[target], "preview.png"], cwd=d, timeout=120
                        )
                        log(job, f"cadgen {door} snapshot rc={rc} out={out[-1000:]} err={err[-1000:]}")
                        if rc == 0 and (d / "preview.png").exists():
                            png = "preview.png"
                            break
                    except FileNotFoundError:
                        log(job, "cadgen-CLI nicht im PATH — Snapshot übersprungen (STL-Viewer im Browser bleibt verfügbar).")
                        break
                    except Exception as e:
                        log(job, f"Snapshot-Fehler (nicht kritisch): {e}")
            if png:
                job["files"]["png"] = png
                log(job, "Snapshot OK: preview.png")
            else:
                log(job, "Kein PNG-Snapshot (Chromium/Playwright evtl. fehlend) — nutze interaktiven STL-Viewer.")
        else:
            log(job, "Snapshot deaktiviert.")

        set_progress(job, 100, "Fertig — Download bereit")
        job["status"] = "done"
        job["finished_at"] = datetime.now().isoformat()
        persist_job(job)
    except HTTPException:
        raise
    except Exception as e:
        job["status"] = "error"
        # KOMPLETTE Fehlerkette (niemals nur letzte Zeile)
        chain = f"{e}\n\n--- Traceback ---\n{traceback.format_exc()}"
        job["error"] = chain[-12000:]
        log(job, f"FEHLER:\n{chain[-8000:]}")
        persist_job(job)


# ---------------------------------------------------------------- API
@app.get("/api/health")
def health():
    return {"ok": True, "service": "text2cad", "time": datetime.now().isoformat()}


@app.get("/api/settings")
def get_settings():
    s = load_settings()
    # Keys maskiert zurückgeben (nie Klartext an den Browser)
    out = dict(s)
    for k in ("openrouter_key", "omnirouter_key", "custom_key"):
        if out.get(k):
            out[k + "_set"] = True
            out[k] = ""
    return out


@app.post("/api/settings")
def post_settings(s: Settings):
    cur = load_settings()
    data = s.model_dump()
    # Leere Keys = behalten (Browser sendet Maskiertes nie zurück)
    for k in ("openrouter_key", "omnirouter_key", "custom_key"):
        if not data[k] and cur.get(k):
            data[k] = cur[k]
    save_settings(data)
    return {"ok": True}


@app.get("/api/models")
async def list_models(provider: str = "openrouter", base_url: str = "", api_key: str = ""):
    settings = load_settings()
    provider = provider if provider in ("openrouter", "omnirouter", "custom") else "openrouter"
    if provider == "openrouter":
        base = (base_url or settings.get("openrouter_url") or OPENROUTER_BASE_DEFAULT).rstrip("/")
        key = api_key or settings.get("openrouter_key", "") or os.environ.get("OPENROUTER_API_KEY", "")
    elif provider == "omnirouter":
        base = (base_url or settings.get("omnirouter_url") or OMNIROUTER_BASE_DEFAULT).rstrip("/")
        key = api_key or settings.get("omnirouter_key", "") or os.environ.get("OMNIROUTER_API_KEY", "")
    else:
        base = (base_url or settings.get("custom_url") or "").rstrip("/")
        key = api_key or settings.get("custom_key", "") or ""
        if not base:
            return {"models": [], "fallback": True, "hint": "Custom-URL fehlt — auf der Einstellungsseite (/settings) hinterlegen (OpenAI-kompatible URL, z.B. http://host:11434/v1)"}
    headers = {}
    if key:
        headers["Authorization"] = f"Bearer {key}"
    try:
        async with httpx.AsyncClient(timeout=20) as c:
            r = await c.get(base.rstrip("/") + "/models", headers=headers)
        if r.status_code >= 400:
            raise RuntimeError(f"HTTP {r.status_code}: {r.text[:2000]}")
        data = r.json()
        items = data.get("data", data.get("models", []))
        models = []
        for m in items:
            mid = m.get("id", m.get("name", ""))
            if mid:
                models.append({"id": mid, "name": m.get("name", mid)})
        models = sorted(models, key=lambda x: x["id"])[:300]
        if not models:
            raise RuntimeError("Leere Modelliste vom Gateway.")
        return {"models": models, "fallback": False, "base": base}
    except Exception as e:
        fb = FALLBACK_MODELS.get(provider, FALLBACK_MODELS["openrouter"])
        return {
            "models": fb,
            "fallback": True,
            "base": base,
            "hint": f"Gateway nicht erreichbar / kein Key — kuratierte Liste. Vollständige Kette: {e}",
        }


@app.post("/api/generate")
async def generate(req: GenerateRequest, bg: BackgroundTasks):
    settings = load_settings()
    # Validierung mit kompletter Kette bei Fehlern
    if req.provider == "custom" and not (req.base_url or settings.get("custom_url")):
        raise HTTPException(400, "Custom-Provider braucht eine Base-URL — auf der Einstellungsseite (/settings) hinterlegen.")
    job_id = uuid.uuid4().hex[:12]
    job = {
        "id": job_id,
        "prompt": req.prompt,
        "provider": req.provider,
        "model": req.model,
        "exports": req.exports,
        "parent_job": req.parent_job or "",
        "status": "queued",
        "progress": 2,
        "stage": "In Warteschlange",
        "logs": [],
        "files": {},
        "created_at": datetime.now().isoformat(),
    }
    JOBS[job_id] = job
    persist_job(job)
    asyncio.create_task(run_job(job_id, req.model_dump()))
    return {"job_id": job_id}


@app.get("/api/jobs/{job_id}")
def job_status(job_id: str):
    job = JOBS.get(job_id)
    if not job:
        # von Platte laden (nach Restart)
        f = JOBS_DIR / job_id / "job.json"
        if f.exists():
            try:
                job = json.loads(f.read_text())
                JOBS[job_id] = job
            except Exception as e:
                raise HTTPException(500, f"Job-Datei korrupt: {e}\n{traceback.format_exc()}")
        else:
            raise HTTPException(404, f"Job {job_id} unbekannt.")
    return JSONResponse(job)


@app.get("/api/jobs")
def jobs_list():
    out = []
    for p in sorted(JOBS_DIR.iterdir(), reverse=True)[:30]:
        f = p / "job.json"
        if f.exists():
            try:
                out.append(json.loads(f.read_text()))
            except Exception:
                pass
    # Memory-Jobs mergen
    for jid, j in JOBS.items():
        if not any(o.get("id") == jid for o in out):
            out.append(j)
    return out[:30]


@app.delete("/api/jobs/{job_id}")
def job_delete(job_id: str):
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", job_id or ""):
        raise HTTPException(400, f"Ungültige Job-ID: {job_id}")
    d = JOBS_DIR / job_id
    if not d.exists() or not d.is_dir():
        raise HTTPException(404, f"Job {job_id} unbekannt.")
    try:
        shutil.rmtree(d)
    except Exception as e:
        raise HTTPException(500, f"Löschen fehlgeschlagen: {e}\n{traceback.format_exc()}")
    JOBS.pop(job_id, None)
    return {"ok": True, "deleted": job_id}


@app.get("/download/{job_id}/{fname}")
def download(job_id: str, fname: str):
    # Path-Traversal-Schutz
    if ".." in fname or "/" in fname:
        raise HTTPException(400, "Ungültiger Dateiname.")
    f = (JOBS_DIR / job_id / fname)
    if not f.exists() or not f.is_file():
        raise HTTPException(404, f"Datei {fname} für Job {job_id} nicht gefunden.")
    media = {
        ".stl": "model/stl", ".step": "application/step", ".stp": "application/step",
        ".3mf": "application/vnd.ms-package.3dmanufacturing-3dmodel+xml",
        ".py": "text/x-python", ".png": "image/png", ".log": "text/plain", ".md": "text/markdown",
    }.get(f.suffix.lower(), "application/octet-stream")
    return FileResponse(str(f), media_type=media, filename=f.name)


# ---------------------------------------------------------------- UI
if STATIC_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


@app.get("/", response_class=HTMLResponse)
def index():
    idx = STATIC_DIR / "index.html"
    if idx.exists():
        return HTMLResponse(idx.read_text())
    return HTMLResponse("<h1>text2CAD</h1><p>static/index.html fehlt.</p>", status_code=500)


@app.get("/settings", response_class=HTMLResponse)
def settings_page():
    idx = STATIC_DIR / "settings.html"
    if idx.exists():
        return HTMLResponse(idx.read_text())
    return HTMLResponse("<h1>text2CAD</h1><p>static/settings.html fehlt.</p>", status_code=500)


@app.get("/models", response_class=HTMLResponse)
def models_page():
    idx = STATIC_DIR / "models.html"
    if idx.exists():
        return HTMLResponse(idx.read_text())
    return HTMLResponse("<h1>text2CAD</h1><p>static/models.html fehlt.</p>", status_code=500)


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=PORT)
