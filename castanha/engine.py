"""Coordenador principal do Castanha (orquestra áudio, transcrição, notas e armazenamento)."""

import os
import json
import uuid
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

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
from castanha.durability import atomic_write, write_json, meeting_lock, sync_directory, file_sha256
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

    def _transcribe_with_fallback(self, audio_path: Path, mode: str, estimated_sec: float):
        """Transcreve com o provedor escolhido e, se ele cair, UMA vez pela VPS.

        Devolve (texto, provedor, erro). Em 05/09/2026 a Groq recusou um áudio
        de 65 MB e o fallback para a VPS rodou duas vezes, uma dentro do
        transcritor e outra aqui, cada uma esperando 10 minutos: vinte minutos
        de "processando" para terminar sem transcrição. O fallback mora só aqui.
        """
        # Import local: o teste troca esta classe por mock em tempo de chamada.
        from castanha.transcription import VpsSshTranscriber, TranscriptionPending

        try:
            transcriber = get_transcriber(estimated_duration_sec=estimated_sec)
        except Exception as e:
            # Sem Groq e sem VPS: falha declarada, e o áudio espera no Bronze.
            return "", "failed", str(e)
        try:
            trans_res = transcriber.transcribe(audio_path, mode=mode)
            return trans_res.text, getattr(trans_res, "provider", transcriber.__class__.__name__), None
        except Exception as e:
            # Pelo nome, e não por isinstance: com a classe trocada por mock,
            # isinstance estoura.
            if isinstance(e, TranscriptionPending) or transcriber.__class__.__name__ == "VpsSshTranscriber":
                return "", "failed", str(e)
            print(f"[Castanha] Erro no transcritor primário: {e}. Tentando VPS local como fallback...", file=sys.stderr)
            host = (self.config.get("transcription", {}) or {}).get("vps_ssh_host") or "zinom-vps-2"
            try:
                trans_res = VpsSshTranscriber(host).transcribe(audio_path, mode=mode)
                return trans_res.text, trans_res.provider, None
            except Exception as err2:
                # Vazio, e não a mensagem de erro: string não vazia ia para a
                # LLM e virava um "resumo" fabricado em cima de um traceback.
                return "", "failed", str(err2)

    def start_recording(
        self,
        mode: Optional[str] = None,
        title: Optional[str] = None,
        meeting_event: Optional[MeetingEvent] = None,
        meeting_slug: Optional[str] = None,
    ) -> Dict[str, Any]:
        state = self.state_mgr.read()
        if state.get("status") in ["recording", "paused", "processing"]:
            return {"status": "error", "message": "Gravação ou finalização já está em andamento. Retome com castanha sync --all."}

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
        if meeting_slug:
            existing = self.storage.get_meeting(meeting_slug)
            if existing:
                current_meeting_info = {
                    "title": existing.get("title") or "Reunião",
                    "slug": meeting_slug,
                    "start": existing.get("when") or datetime.now().isoformat(),
                    "attendees": existing.get("attendees", []),
                }
        if not current_meeting_info:
            if meeting_event:
                current_meeting_info = (
                    meeting_event.to_dict() if hasattr(meeting_event, "to_dict") else dict(meeting_event)
                )
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
            "target_meeting_slug": meeting_slug,
            "capture_slug": None,
            "capture_job_id": None,
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
        if state.get("status") not in ["recording", "paused", "processing"]:
            return {"status": "error", "message": "Nenhuma gravação em andamento para finalizar."}

        self.state_mgr.write({"status": "processing"})
        notify("Finalizando Reunião ⏳", "Processando transcrição e gerando notas...")

        pid = state.get("pid")
        audio_path = Path(state.get("audio_path", ""))

        # Finaliza processo do áudio
        if pid and state.get("status") in ("recording", "paused"):
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

        self.state_mgr.write({"pid": None})
        if state.get("capture_slug") and state.get("capture_job_id"):
            saved = (self.storage.bronze_dir / state["capture_slug"] /
                     f"capture_{state['capture_job_id']}{audio_path.suffix}")
            if saved.exists():
                audio_path = saved

        if not audio_path.exists() or audio_path.stat().st_size == 0:
            self.state_mgr.reset()
            return {"status": "error", "message": "Arquivo de áudio não foi gravado ou está vazio."}

        # O identificador sobrevive ao processo antes de copiar o original.
        current_meeting = state.get("current_meeting") or {}
        title = current_meeting.get("title") or "Reunião"
        slug = state.get("capture_slug") or state.get("target_meeting_slug") or self.storage.create_meeting_slug(title)
        job_id = state.get("capture_job_id") or uuid.uuid4().hex
        self.state_mgr.write({"capture_slug": slug, "capture_job_id": job_id})
        bronze = self.storage.bronze_dir / slug
        bronze.mkdir(parents=True, exist_ok=True)
        sync_directory(bronze.parent)
        with meeting_lock(bronze):
            jobs = bronze / ".jobs"
            jobs.mkdir(exist_ok=True)
            sync_directory(bronze)
            job_file = jobs / f"{job_id}.json"
            if not job_file.exists():
                # Capturas legadas não são reescritas nem inferidas como mock.
                base = jobs / "base_transcript.txt"
                if not base.exists():
                    transcript = bronze / "transcript_raw.txt"
                    atomic_write(base, transcript.read_text(encoding="utf-8") if transcript.exists() else "")
                durable_audio = bronze / f"capture_{job_id}{audio_path.suffix}"
                atomic_write(durable_audio, audio_path)
                job = {"id": job_id, "audio_path": str(durable_audio), "sha256": file_sha256(durable_audio), "state": state,
                       "stage": "pending", "recorded_at": state.get("started_at") or datetime.now().isoformat()}
                write_json(job_file, job)
                metadata = self.storage._read_bronze_metadata(slug) or {
                    "slug": slug, "title": title, "recorded_at": job["recorded_at"],
                    "mode": state.get("mode", "dual"), "calendar_event": current_meeting,
                    "mic_muted_at_start": state.get("mic_muted_at_start"), "recordings": [],
                }
                metadata["processing_status"] = "pending"
                metadata.setdefault("transcription_provider", "pending")
                write_json(bronze / "metadata.json", metadata)

        result = self.process_pending(slug)
        self.state_mgr.write({"status": "idle", "pid": None, "audio_path": None,
                              "current_meeting": None, "capture_slug": None, "capture_job_id": None,
                              "elapsed_seconds": 0, "last_result": result["result"]})
        if result["status"] == "partial":
            notify("Gravação preservada, processamento pendente", title, timeout=10000)
        else:
            notify("Notas Prontas! 🌰", title)
        return result

    def process_pending(self, slug: str) -> Dict[str, Any]:
        """Retoma checkpoints do Bronze sem recapturar nem duplicar transcrições."""
        bronze = self.storage.bronze_dir / slug
        with meeting_lock(bronze):
            return self._process_pending_locked(slug)

    def _process_pending_locked(self, slug: str) -> Dict[str, Any]:
        bronze = self.storage.bronze_dir / slug
        metadata = self.storage._read_bronze_metadata(slug)
        jobs_dir = bronze / ".jobs"
        jobs = [(p, json.loads(p.read_text(encoding="utf-8"))) for p in jobs_dir.glob("*.json")]
        jobs.sort(key=lambda item: (item[1]["recorded_at"], item[1]["id"]))
        if not metadata and jobs:
            first = jobs[0][1]
            state = first["state"]
            metadata = {"slug": slug, "title": (state.get("current_meeting") or {}).get("title") or "Reunião",
                        "recorded_at": first["recorded_at"], "calendar_event": state.get("current_meeting") or {},
                        "mode": state.get("mode", "dual"), "recordings": []}
        errors = []
        for job_file, job in jobs:
            if job["stage"] != "pending":
                continue
            source = Path(job["audio_path"])
            state = job["state"]
            mode = state.get("mode", "dual")
            levels = measure_channel_levels(source, mode=mode)
            audio_status = classify_audio(levels)
            duration = probe_duration_seconds(source)
            job.update(audio_status=audio_status, duration_seconds=duration or state.get("elapsed_seconds", 0),
                       audio_levels=[{"canal": ch.channel, "origem": ch.label, "mean_db": ch.mean_db,
                                      "max_db": ch.max_db, "silencio": ch.silent} for ch in levels])
            try:
                if audio_status == "sem_audio":
                    job.update(transcript="", provider="nenhum (áudio em silêncio)")
                else:
                    transcriber = get_transcriber(estimated_duration_sec=duration or state.get("elapsed_seconds") or 60)
                    try:
                        transcription = transcriber.transcribe(source, mode=mode)
                    except Exception as exc:
                        from castanha.transcription import VpsSshTranscriber, TranscriptionPending
                        # Job remoto já aceito ou indisponível: próximo sync retoma.
                        if isinstance(exc, TranscriptionPending) or transcriber.__class__.__name__ == "VpsSshTranscriber":
                            raise
                        host = self.config.get("transcription", {}).get("vps_ssh_host", "zinom-vps-2")
                        transcription = VpsSshTranscriber(host).transcribe(source, mode=mode)
                    if not transcription.text.strip():
                        raise RuntimeError("Transcrição vazia; áudio preservado para nova tentativa")
                    job.update(transcript=transcription.text, provider=transcription.provider)
                job.update(stage="transcribed", error=None)
            except Exception as exc:
                job.update(error=str(exc), provider="failed")
                errors.append(f"a transcrição falhou ({exc})")
            write_json(job_file, job)

        # Reconstrói sempre a mesma transcrição a partir de checkpoints imutáveis.
        base = jobs_dir / "base_transcript.txt"
        legacy_provider = metadata.get("legacy_transcription_provider", metadata.get("transcription_provider"))
        metadata["legacy_transcription_provider"] = legacy_provider
        parts = [base.read_text(encoding="utf-8")] if base.exists() and legacy_provider != "mock" else []
        records = [r for r in metadata.get("recordings", []) if not r.get("job_id")]
        for record in records:
            record.setdefault("transcription_provider", legacy_provider)
            record.setdefault("id", record.get("filename"))
        for _, job in jobs:
            if job.get("transcript") and job.get("provider") != "mock":
                parts.append(job["transcript"])
            source = Path(job["audio_path"])
            records.append({"id": source.name, "filename": source.name, "path": str(source),
                            "job_id": job["id"], "sha256": job.get("sha256"), "recorded_at": job["recorded_at"],
                            "size_bytes": source.stat().st_size if source.exists() else 0,
                            "duration_seconds": job.get("duration_seconds", 0),
                            "audio_status": job.get("audio_status", "desconhecido"),
                            "transcribed": job.get("stage") in ("transcribed", "done"),
                            "transcription_error": job.get("error"),
                            "transcription_provider": job.get("provider")})
        transcript = "\n\n".join(p for p in parts if p)
        atomic_write(bronze / "transcript_raw.txt", transcript)
        last_job = jobs[-1][1]
        memory_records = [r for r in records if r.get("transcription_provider") not in ("mock", "failed")
                          and r.get("audio_status") != "sem_audio"]
        providers = {r.get("transcription_provider") for r in memory_records if r.get("transcription_provider")}
        provider = (next(iter(providers)) if len(providers) == 1 else "mixed") if providers else last_job.get("provider", "failed")
        statuses = {r.get("audio_status") for r in memory_records}
        audio_status = ("ok" if "ok" in statuses else "mic_mudo" if "mic_mudo" in statuses
                        else last_job.get("audio_status", "desconhecido"))
        metadata.update(recordings=records, recordings_count=len(records),
                        duration_seconds=sum(r.get("duration_seconds", 0) for r in records),
                        bronze_audio_file=last_job["audio_path"], transcription_provider=provider,
                        memory_recording_ids=[r["id"] for r in memory_records],
                        transcription_error=last_job.get("error"), audio_status=audio_status,
                        audio_diagnostico=AUDIO_STATUS_MESSAGES.get(audio_status, ""),
                        audio_levels=last_job.get("audio_levels", []),
                        processing_status="pending" if errors else "complete")
        write_json(bronze / "metadata.json", metadata)
        silver_content = self.summarizer.generate_silver(metadata, transcript)
        silver_path = self.storage.save_silver(slug, silver_content)
        gold_data = self.summarizer.generate_gold(metadata, silver_content, transcript)
        gold_path = self.storage.save_gold(slug, gold_data)
        zinom_status = self.zinom.ingest_meeting(
            metadata, silver_content, gold_data,
            on_remember=lambda receipt: self.storage.record_zinom_result(slug, receipt),
        )
        self.storage.record_zinom_result(slug, zinom_status)
        if not errors:
            for job_file, job in jobs:
                job["stage"] = "done"
                write_json(job_file, job)
        if audio_status in ("sem_audio", "mic_mudo"):
            errors.append(AUDIO_STATUS_MESSAGES[audio_status])
        if any(r.get("transcription_provider") == "mock" for r in records):
            errors.append("Transcrição simulada preservada separadamente, não enviada à memória")
        if zinom_status.get("facts_status") == "pending_lineage":
            errors.append(zinom_status["reason"])
        if zinom_status.get("status") == "error":
            errors.extend(zinom_status.get("errors", []))
        summary = {"slug": slug, "title": metadata["title"], "bronze_dir": str(bronze),
                   "silver_file": str(silver_path), "gold_file": str(gold_path),
                   "audio_status": audio_status, "audio_diagnostico": metadata["audio_diagnostico"],
                   "transcription_provider": provider, "transcription_error": last_job.get("error"),
                   "zinom": zinom_status, "problemas": errors}
        return {"status": "partial" if errors else "success", "result": summary}

    def toggle_recording(self) -> Dict[str, Any]:
        state = self.state_mgr.read()
        if state.get("status") in ["recording", "paused"]:
            return self.stop_recording()
        else:
            return self.start_recording()

    def delete_recording(self, slug: str, recording_name: Optional[str] = None) -> Dict[str, Any]:
        """Apaga uma gravação de áudio de uma reunião preservando suas notas e transcrição."""
        res = self.storage.delete_recording(slug, recording_name)
        if res.get("status") == "ok":
            state = self.state_mgr.read()
            last_res = state.get("last_result")
            if last_res and last_res.get("slug") == slug:
                if res.get("remaining_count", 0) == 0:
                    last_res["audio_status"] = "audio_apagado"
                    last_res["audio_diagnostico"] = "Gravação de áudio apagada (notas e transcrição preservadas)"
                    self.state_mgr.write({"last_result": last_res})
        return res

    def reprocess_meeting(self, slug: str) -> Dict[str, Any]:
        """Roda de novo a esteira (transcrição, Silver, Gold, Zinom) de uma reunião do Bronze.

        É a segunda chance da gravação que ficou sem transcrição: sem internet
        na hora do `stop`, Groq fora do ar, VPS lenta. Só transcreve as
        gravações que ainda não têm texto, anexa o que conseguir e nunca apaga
        o que já estava lá: se falhar de novo, o Bronze fica como estava. A
        nota do Zinom é editada pelo id, não duplicada.
        """
        bronze_dir = self.storage.bronze_dir / slug
        if not bronze_dir.exists():
            return {"status": "error", "message": f"Reunião '{slug}' não encontrada no Bronze."}

        if any((bronze_dir / ".jobs").glob("*.json")):
            # O botão legado também retoma os checkpoints novos, sem retranscrever
            # nem anexar uma segunda cópia do texto ao caminho paralelo antigo.
            result = self.process_pending(slug)
            state = self.state_mgr.read()
            if state.get("status") == "processing" and state.get("capture_slug") == slug:
                self.state_mgr.write({"status": "idle", "pid": None, "audio_path": None,
                                      "capture_slug": None, "capture_job_id": None,
                                      "current_meeting": None, "last_result": result["result"]})
            return result
        with meeting_lock(bronze_dir):
            return self._reprocess_legacy_locked(slug)

    def _reprocess_legacy_locked(self, slug: str) -> Dict[str, Any]:
        bronze_dir = self.storage.bronze_dir / slug

        meta = self.storage._read_bronze_metadata(slug)
        title = meta.get("title") or slug
        mode = meta.get("mode") or "dual"

        recordings = self.storage.list_meeting_recordings(slug)
        if not recordings:
            return {"status": "error", "message": f"Nenhum arquivo de áudio encontrado para a reunião '{slug}'."}

        texto_antes = self.storage.read_transcript(slug)
        # Sem marca (reunião antiga): se já há texto, ela já foi transcrita.
        pendentes = [
            r for r in recordings
            if r.get("transcribed") is False or (r.get("transcribed") is None and not texto_antes.strip())
        ]

        notify("Reprocessando Reunião ⏳", f"{title}\nEnviando para transcrição e gerando notas...")

        erros: List[str] = []
        provider_final = None
        novo_texto = False
        for indice, rec in enumerate(recordings):
            if rec not in pendentes:
                continue
            audio_path = Path(rec["path"])
            if not audio_path.exists() or audio_path.stat().st_size == 0:
                erros.append(f"{rec['filename']}: arquivo não existe ou está vazio")
                self.storage.update_recording(slug, rec["filename"], transcribed=False,
                                              transcription_error="arquivo não existe ou está vazio")
                continue

            levels = measure_channel_levels(audio_path, mode=mode)
            audio_status = classify_audio(levels)
            dur = probe_duration_seconds(audio_path) or rec.get("duration_seconds") or 0
            if indice == 0:
                meta = self.storage._read_bronze_metadata(slug)
                meta["audio_status"] = audio_status
                meta["audio_diagnostico"] = AUDIO_STATUS_MESSAGES.get(audio_status, "")
                meta["audio_levels"] = [
                    {"canal": ch.channel, "origem": ch.label, "mean_db": ch.mean_db,
                     "max_db": ch.max_db, "silencio": ch.silent}
                    for ch in levels
                ]
                self.storage.write_bronze_metadata(slug, meta)

            if audio_status == "sem_audio":
                # Repetir não inventa fala: marca como resolvida.
                self.storage.update_recording(slug, rec["filename"], transcribed=True,
                                              transcription_error=None, audio_status=audio_status,
                                              duration_seconds=dur)
                provider_final = provider_final or "nenhum (áudio em silêncio)"
                continue

            texto, provider, erro = self._transcribe_with_fallback(audio_path, mode, dur or 60.0)
            if texto.strip():
                self.storage.append_transcript(slug, rec["filename"], texto)
                self.storage.update_recording(slug, rec["filename"], transcribed=True,
                                              transcription_error=None, audio_status=audio_status,
                                              duration_seconds=dur)
                provider_final = provider
                novo_texto = True
            else:
                motivo = erro or "transcrição vazia"
                self.storage.update_recording(slug, rec["filename"], transcribed=False,
                                              transcription_error=motivo, audio_status=audio_status,
                                              duration_seconds=dur)
                erros.append(f"{rec['filename']}: {motivo}")

        meta = self.storage._read_bronze_metadata(slug)
        transcript = self.storage.read_transcript(slug)
        recordings = self.storage.list_meeting_recordings(slug)
        soma = sum(float(r.get("duration_seconds") or 0) for r in recordings)
        if soma > 0:
            meta["duration_seconds"] = soma
        if erros and not transcript.strip():
            meta["transcription_provider"] = "failed"
        elif provider_final:
            meta["transcription_provider"] = provider_final
        meta["transcription_error"] = "; ".join(erros) if erros else None
        self.storage.write_bronze_metadata(slug, meta)

        silver_path = self.storage.silver_dir / f"{slug}.md"
        gold_path = self.storage.gold_dir / f"{slug}.json"
        zinom_status: Dict[str, Any] = meta.get("zinom") or {}
        # Nota nova só com texto novo, ou quando nada estava pendente (aí o
        # pedido é refazer as notas). Falhar de novo não mexe na nota que existe.
        refazer_notas = transcript.strip() and (novo_texto or not pendentes)
        if refazer_notas:
            # 4. Silver, 5. Gold, 6. Zinom (editando a nota anterior, se houver)
            silver_content = self.summarizer.generate_silver(meta, transcript)
            silver_path = self.storage.save_silver(slug, silver_content)
            gold_data = self.summarizer.generate_gold(meta, silver_content, transcript)
            gold_path = self.storage.save_gold(slug, gold_data)
            anterior = (meta.get("zinom") or {}).get("remember_id")
            zinom_status = self.zinom.ingest_meeting(
                meta, silver_content, gold_data, previous_remember_id=anterior,
                on_remember=lambda receipt: self.storage.record_zinom_result(slug, receipt))
            self.storage.record_zinom_result(slug, zinom_status)
            meta = self.storage._read_bronze_metadata(slug)

        provider_name = meta.get("transcription_provider") or "failed"
        result_summary = {
            "slug": slug,
            "title": title,
            "bronze_dir": str(bronze_dir),
            "silver_file": str(silver_path),
            "gold_file": str(gold_path),
            "audio_status": meta.get("audio_status", "ok"),
            "audio_diagnostico": meta.get("audio_diagnostico", ""),
            "transcription_provider": provider_name,
            "transcription_error": meta.get("transcription_error"),
            "zinom": zinom_status,
        }

        # Atualiza o state caso seja a última reunião
        state = self.state_mgr.read()
        last_res = state.get("last_result")
        if last_res and last_res.get("slug") == slug:
            self.state_mgr.write({"last_result": result_summary})

        problemas = []
        if erros:
            problemas.append(f"a transcrição falhou ({'; '.join(erros)})")
            notify("Falha ao reprocessar ⚠️", f"{title}\nNão foi possível transcrever o áudio.", timeout=10000)
        else:
            notify("Reunião Reprocessada! 🌰", f"{title}\nNotas e fatos atualizados.")

        if meta.get("audio_status") in ("sem_audio", "mic_mudo"):
            problemas.append(AUDIO_STATUS_MESSAGES.get(meta["audio_status"], meta["audio_status"]))
        if isinstance(zinom_status, dict) and zinom_status.get("status") == "error":
            problemas.extend(zinom_status.get("errors", []))
        result_summary["problemas"] = problemas

        return {"status": "partial" if problemas else "success", "result": result_summary}
