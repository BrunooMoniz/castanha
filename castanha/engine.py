"""Coordenador principal do Castanha (orquestra áudio, transcrição, notas e armazenamento)."""

import os
import subprocess
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Optional

from castanha.audio import (
    AUDIO_STATUS_MESSAGES,
    AudioRecorder,
    RecordingResult,
    classify_audio,
    is_default_source_muted,
    measure_channel_levels,
    probe_duration_seconds,
)
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

        # O microfone mudo no teclado é invisível para o ffmpeg: ele grava
        # silêncio digital sem reclamar. Avisar aqui é a diferença entre perder
        # a reunião e perder dois segundos.
        mic_muted = is_default_source_muted()
        if mic_muted:
            notify(
                "Microfone mudo! 🔇",
                "O microfone está mudo no sistema ou no teclado. Desmute antes de falar, "
                "senão a gravação sai em silêncio.",
                timeout=10000,
            )

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
            "mic_muted_at_start": mic_muted,
            "error": None,
        })

        mode_label = "Microfone + Chamada" if chosen_mode == "dual" else "Somente Microfone"
        notify("Gravação Iniciada 🌰", f"{meeting_title}\nModo: {mode_label}")
        return {
            "status": "recording",
            "pid": proc.pid,
            "title": meeting_title,
            "mic_muted": mic_muted,
        }

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
                # Fallback se o FFmpeg/PipeWire travar: força SIGKILL
                if os.path.exists(f"/proc/{pid}"):
                    os.kill(pid, signal.SIGKILL)
                    for _ in range(10):
                        if not os.path.exists(f"/proc/{pid}"):
                            break
                        time.sleep(0.1)
            except ProcessLookupError:
                pass
            except Exception:
                pass

        if not audio_path.exists() or audio_path.stat().st_size == 0:
            self.state_mgr.reset()
            return {"status": "error", "message": "Arquivo de áudio não foi gravado ou está vazio."}

        current_meeting = state.get("current_meeting") or {}
        title = current_meeting.get("title") or "Reunião"
        mode = state.get("mode", "dual")

        # 1. Sanidade do áudio antes de qualquer coisa cara.
        # Whisper alucina em cima de silêncio ("Thank you. Thank you."), então
        # gravação muda não vai para transcrição nenhuma.
        levels = measure_channel_levels(audio_path, mode=mode)
        audio_status = classify_audio(levels)
        audio_levels = [
            {"canal": ch.channel, "origem": ch.label, "mean_db": ch.mean_db,
             "max_db": ch.max_db, "silencio": ch.silent}
            for ch in levels
        ]
        real_duration = probe_duration_seconds(audio_path)

        # 2. Transcrição (Whisper na Groq / VPS)
        transcription_error = None
        if audio_status == "sem_audio":
            raw_transcript = ""
            provider_name = "nenhum (áudio em silêncio)"
        else:
            estimated = real_duration or state.get("elapsed_seconds") or 60
            transcriber = get_transcriber(estimated_duration_sec=estimated)
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
                    # Vazio, e não a mensagem de erro: string não vazia ia para a
                    # LLM e virava um "resumo" fabricado em cima de um traceback.
                    raw_transcript = ""
                    transcription_error = str(err2)
                    provider_name = "failed"

        # 3. Metadados e Bronze
        slug = self.storage.create_meeting_slug(title)
        metadata = {
            "slug": slug,
            "title": title,
            "recorded_at": state.get("started_at") or datetime.now().isoformat(),
            # Duração medida no arquivo: o cronômetro do daemon fica em zero
            # quando o daemon não está rodando.
            "duration_seconds": real_duration if real_duration is not None else state.get("elapsed_seconds", 0),
            "mode": mode,
            "transcription_provider": provider_name,
            "audio_status": audio_status,
            "audio_diagnostico": AUDIO_STATUS_MESSAGES.get(audio_status, ""),
            "audio_levels": audio_levels,
            "transcription_error": transcription_error,
            "mic_muted_at_start": state.get("mic_muted_at_start"),
            "calendar_event": current_meeting,
        }

        bronze_dir = self.storage.save_bronze(slug, audio_path, metadata, raw_transcript)

        # 4. Processamento Silver (Markdown)
        silver_content = self.summarizer.generate_silver(metadata, raw_transcript)
        silver_path = self.storage.save_silver(slug, silver_content)

        # 5. Processamento Gold (Fatos para Zinom / LLM Wiki)
        gold_data = self.summarizer.generate_gold(metadata, silver_content, raw_transcript)
        gold_path = self.storage.save_gold(slug, gold_data)

        # 6. Ingestão Zinom (se habilitado)
        zinom_status = self.zinom.ingest_meeting(metadata, silver_content, gold_data)
        # O resultado fica NO METADATA, e não só no estado da sessão: é por ele
        # que o `castanha sync` sabe o que ficou para trás e qual nota editar.
        self.storage.record_zinom_result(slug, zinom_status)

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
            "audio_status": audio_status,
            "audio_diagnostico": AUDIO_STATUS_MESSAGES.get(audio_status, ""),
            "transcription_provider": provider_name,
            "transcription_error": transcription_error,
            "zinom": zinom_status,
        }

        self.state_mgr.write({
            "status": "idle",
            "pid": None,
            "audio_path": None,
            "current_meeting": None,
            "mic_muted_at_start": None,
            "last_result": result_summary,
            "elapsed_seconds": 0,
        })

        # A notificação diz o que realmente aconteceu: "Notas prontas" em cima de
        # uma gravação muda foi exatamente o que enganou no teste de 04/09.
        if audio_status == "sem_audio":
            notify("Gravação sem áudio 🔇", f"{title}\n{AUDIO_STATUS_MESSAGES['sem_audio']}", timeout=10000)
        elif audio_status == "mic_mudo":
            notify("Notas prontas, sem o seu microfone 🔇", f"{title}\n{AUDIO_STATUS_MESSAGES['mic_mudo']}", timeout=10000)
        elif provider_name == "failed":
            notify("Transcrição falhou ⚠️", f"{title}\nO áudio está salvo no Bronze, mas não há notas.", timeout=10000)
        elif audio_status == "desconhecido":
            notify("Notas prontas, áudio não medido 🌰", f"Reunião: {title}\nNão deu para medir os níveis do áudio.", timeout=8000)
        else:
            notify("Notas Prontas! 🌰", f"Reunião: {title}\nSalvo em {silver_path.name}")

        problemas = []
        if provider_name == "failed":
            problemas.append(f"a transcrição falhou ({transcription_error})")
        if audio_status in ("sem_audio", "mic_mudo"):
            problemas.append(AUDIO_STATUS_MESSAGES.get(audio_status, audio_status))
        if isinstance(zinom_status, dict) and zinom_status.get("status") == "error":
            problemas.extend(zinom_status.get("errors", []))
        result_summary["problemas"] = problemas

        # O áudio está no Bronze de qualquer jeito, mas "sucesso" com transcrição
        # falha é o tipo de verde mentiroso que este projeto não pode ter.
        return {"status": "partial" if problemas else "success", "result": result_summary}

    def toggle_recording(self) -> Dict[str, Any]:
        state = self.state_mgr.read()
        if state.get("status") in ["recording", "paused"]:
            return self.stop_recording()
        else:
            return self.start_recording()
