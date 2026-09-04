"""Coordenador principal do Castanha (orquestra áudio, transcrição, notas e armazenamento)."""

import os
import subprocess
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Optional

from castanha.audio import AudioRecorder, RecordingResult
from castanha.calendar import MeetingEvent
from castanha.config import load_config
from castanha.state import StateManager
from castanha.storage import MeetingStorage
from castanha.summarizer import MeetingSummarizer
from castanha.transcription import get_transcriber
from castanha.zinom_adapter import ZinomAdapter

def notify(title: str, message: str, actions: Optional[list] = None, timeout: int = 5000) -> Optional[str]:
    cmd = ["notify-send", "-a", "Castanha", "-i", "audio-input-microphone", title, message, "-t", str(timeout)]
    if actions:
        for act_id, act_label in actions:
            cmd.extend(["-A", f"{act_id}={act_label}"])
        try:
            res = subprocess.run(cmd, capture_output=True, text=True)
            return res.stdout.strip()
        except Exception:
            return None
    else:
        subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return None

class CastanhaEngine:
    def __init__(self):
        self.config = load_config()
        self.state_mgr = StateManager()
        self.storage = MeetingStorage()
        self.recorder = AudioRecorder()
        self.summarizer = MeetingSummarizer()
        self.zinom = ZinomAdapter()

    def get_status(self) -> Dict[str, Any]:
        return self.state_mgr.read()

    def start_recording(
        self,
        mode: Optional[str] = None,
        title: Optional[str] = None,
        meeting_event: Optional[MeetingEvent] = None,
    ) -> Dict[str, Any]:
        state = self.state_mgr.read()
        if state.get("status") in ["recording", "paused"]:
            return {"status": "error", "message": "Gravação já está em andamento."}

        audio_cfg = self.config.get("audio", {})
        chosen_mode = mode or audio_cfg.get("default_mode", "dual")
        bitrate = audio_cfg.get("bitrate", "64k")

        temp_audio = Path(f"/tmp/castanha_rec_{int(time.time())}.ogg")
        proc = self.recorder.start(temp_audio, mode=chosen_mode, bitrate=bitrate)

        current_meeting_info = None
        if meeting_event:
            current_meeting_info = meeting_event.to_dict()
        elif title:
            current_meeting_info = {
                "title": title,
                "start": datetime.now().isoformat(),
                "attendees": [],
            }
        else:
            # Tenta pegar da próxima reunião do calendário se estiver no horário
            next_m = state.get("next_meeting")
            if next_m:
                current_meeting_info = next_m

        meeting_title = current_meeting_info.get("title") if current_meeting_info else "Reunião Avulsa"

        self.state_mgr.write({
            "status": "recording",
            "pid": proc.pid,
            "audio_path": str(temp_audio),
            "mode": chosen_mode,
            "started_at": datetime.now().isoformat(),
            "elapsed_seconds": 0,
            "current_meeting": current_meeting_info,
            "error": None,
        })

        mode_label = "Microfone + Chamada" if chosen_mode == "dual" else "Somente Microfone"
        notify("Gravação Iniciada 🌰", f"{meeting_title}\nModo: {mode_label}")
        return {"status": "recording", "pid": proc.pid, "title": meeting_title}

    def pause_recording(self) -> Dict[str, Any]:
        state = self.state_mgr.read()
        if state.get("status") != "recording":
            return {"status": "error", "message": "Nenhuma gravação ativa para pausar."}

        pid = state.get("pid")
        if pid:
            try:
                import signal
                os.kill(pid, signal.SIGSTOP)
            except Exception as e:
                return {"status": "error", "message": str(e)}

        self.state_mgr.write({"status": "paused"})
        notify("Gravação Pausada ⏸️", "Clique em Retomar quando continuar.")
        return {"status": "paused"}

    def resume_recording(self) -> Dict[str, Any]:
        state = self.state_mgr.read()
        if state.get("status") != "paused":
            return {"status": "error", "message": "A gravação não está pausada."}

        pid = state.get("pid")
        if pid:
            try:
                import signal
                os.kill(pid, signal.SIGCONT)
            except Exception as e:
                return {"status": "error", "message": str(e)}

        self.state_mgr.write({"status": "recording"})
        notify("Gravação Retomada ▶️", "Capturando áudio da reunião.")
        return {"status": "recording"}

    def stop_recording(self) -> Dict[str, Any]:
        state = self.state_mgr.read()
        if state.get("status") not in ["recording", "paused"]:
            return {"status": "error", "message": "Nenhuma gravação em andamento para finalizar."}

        self.state_mgr.write({"status": "processing"})
        notify("Finalizando Reunião ⏳", "Processando transcrição e gerando notas...")

        pid = state.get("pid")
        audio_path = Path(state.get("audio_path", ""))

        # Finaliza processo do áudio
        if pid:
            try:
                import signal
                os.kill(pid, signal.SIGINT)
                for _ in range(30):
                    if not os.path.exists(f"/proc/{pid}"):
                        break
                    time.sleep(0.1)
            except Exception:
                pass

        if not audio_path.exists() or audio_path.stat().st_size == 0:
            self.state_mgr.reset()
            return {"status": "error", "message": "Arquivo de áudio não foi gravado ou está vazio."}

        current_meeting = state.get("current_meeting") or {}
        title = current_meeting.get("title") or "Reunião"
        mode = state.get("mode", "dual")

        # 1. Transcrição (Whisper na Groq / VPS)
        transcriber = get_transcriber(estimated_duration_sec=state.get("elapsed_seconds", 60))
        try:
            trans_res = transcriber.transcribe(audio_path, mode=mode)
            raw_transcript = trans_res.text
            provider_name = getattr(trans_res, "provider", getattr(transcriber, "__class__", {}).__name__)
        except Exception as e:
            print(f"[Castanha] Erro no transcritor primário: {e}. Tentando VPS local como fallback...")
            try:
                from castanha.transcription import VpsSshTranscriber
                trans_res = VpsSshTranscriber().transcribe(audio_path, mode=mode)
                raw_transcript = trans_res.text
                provider_name = trans_res.provider
            except Exception as err2:
                raw_transcript = f"[Erro na transcrição: {err2}]"
                provider_name = "failed"

        # 2. Metadados e Bronze
        slug = self.storage.create_meeting_slug(title)
        metadata = {
            "slug": slug,
            "title": title,
            "recorded_at": state.get("started_at") or datetime.now().isoformat(),
            "duration_seconds": state.get("elapsed_seconds", 0),
            "mode": mode,
            "transcription_provider": provider_name,
            "calendar_event": current_meeting,
        }

        bronze_dir = self.storage.save_bronze(slug, audio_path, metadata, raw_transcript)

        # 3. Processamento Silver (Markdown)
        silver_content = self.summarizer.generate_silver(metadata, raw_transcript)
        silver_path = self.storage.save_silver(slug, silver_content)

        # 4. Processamento Gold (Fatos para Zinom / LLM Wiki)
        gold_data = self.summarizer.generate_gold(metadata, silver_content, raw_transcript)
        gold_path = self.storage.save_gold(slug, gold_data)

        # 5. Ingestão Zinom (se habilitado)
        zinom_status = self.zinom.ingest_meeting(metadata, silver_content, gold_data)

        # Limpa arquivo temporário
        try:
            if audio_path.exists():
                audio_path.unlink()
        except Exception:
            pass

        result_summary = {
            "slug": slug,
            "title": title,
            "bronze_dir": str(bronze_dir),
            "silver_file": str(silver_path),
            "gold_file": str(gold_path),
            "zinom": zinom_status,
        }

        self.state_mgr.write({
            "status": "idle",
            "pid": None,
            "audio_path": None,
            "current_meeting": None,
            "last_result": result_summary,
            "elapsed_seconds": 0,
        })

        notify("Notas Prontas! 🌰", f"Reunião: {title}\nSalvo em {silver_path.name}")
        return {"status": "success", "result": result_summary}

    def toggle_recording(self) -> Dict[str, Any]:
        state = self.state_mgr.read()
        if state.get("status") in ["recording", "paused"]:
            return self.stop_recording()
        else:
            return self.start_recording()
