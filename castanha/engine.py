"""Coordenador principal do Castanha (orquestra áudio, transcrição, notas e armazenamento)."""

import os
import json
import uuid
import subprocess
import sys
import time
from datetime import datetime, timezone
from dataclasses import asdict
from pathlib import Path
from typing import Any, Dict, List, Optional

from castanha.audio import (
    AudioRecorder,
    RecordingResult,
    classify_audio,
    is_default_source_muted,
    measure_channel_levels,
    probe_duration_seconds,
)
from castanha.calendar import MeetingEvent
from castanha.channel_transcription import transcribe_dual
from castanha.capture_gate import capture_start
from castanha.config import load_config
from castanha.durability import atomic_write, write_json, meeting_lock, sync_directory, file_sha256
from castanha.i18n import audio_status_message, t
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
        except OSError as exc:
            print(t("notify.unavailable", exc=type(exc).__name__), file=sys.stderr)
            return None
    else:
        try:
            subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except OSError as exc:
            print(t("notify.unavailable", exc=type(exc).__name__), file=sys.stderr)
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
        return self.storage.status_projection(self.state_mgr.read())

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
        except TranscriptionPending as e:
            # A configuração/worker que precisa ser retomado continua
            # pendente; não é uma falha terminal nem autoriza perder o áudio.
            return "", "pending", str(e)
        except Exception as e:
            # Sem Groq e sem VPS: falha declarada, e o áudio espera no Bronze.
            return "", "failed", str(e)
        try:
            trans_res = transcriber.transcribe(audio_path, mode=mode)
            return trans_res.text, getattr(trans_res, "provider", transcriber.__class__.__name__), None
        except Exception as e:
            # Pelo nome, e não por isinstance: com a classe trocada por mock,
            # isinstance estoura.
            if isinstance(e, TranscriptionPending):
                return "", "pending", str(e)
            if transcriber.__class__.__name__ == "VpsSshTranscriber":
                return "", "failed", str(e)
            print(f"[Castanha] Erro no transcritor primário: {e}. Tentando VPS local como fallback...", file=sys.stderr)
            host = (self.config.get("transcription", {}) or {}).get("vps_ssh_host") or "zinom-vps-2"
            try:
                trans_res = VpsSshTranscriber(host).transcribe(audio_path, mode=mode)
                return trans_res.text, trans_res.provider, None
            except TranscriptionPending as err2:
                return "", "pending", str(err2)
            except Exception as err2:
                # Vazio, e não a mensagem de erro: string não vazia ia para a
                # LLM e virava um "resumo" fabricado em cima de um traceback.
                return "", "failed", str(err2)

    @capture_start
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
                t("notify.mic_muted_title"),
                t("notify.mic_muted_body"),
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

        mode_label = t("mode.dual") if chosen_mode == "dual" else t("mode.mic_only")
        notify(t("notify.started_title"), t("notify.started_body", title=meeting_title, mode_label=mode_label))
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
        notify(t("notify.paused_title"), t("notify.paused_body"))
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
        notify(t("notify.resumed_title"), t("notify.resumed_body"))
        return {"status": "recording"}

    def stop_recording(self) -> Dict[str, Any]:
        state = self.state_mgr.read()
        if state.get("status") not in ["recording", "paused", "processing"]:
            return {"status": "error", "message": "Nenhuma gravação em andamento para finalizar."}

        self.state_mgr.write({"status": "processing", "processing_pid": os.getpid()})
        notify(t("notify.finishing_title"), t("notify.finishing_body"))

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
        self.state_mgr.write({"status": "idle", "pid": None, "processing_pid": None, "audio_path": None,
                              "current_meeting": None, "capture_slug": None, "capture_job_id": None,
                              "elapsed_seconds": 0, "last_result": result["result"]})
        if result["status"] == "partial":
            notify(t("notify.preserved_title"), title, timeout=10000)
        else:
            notify(t("notify.notes_ready_title"), title)
        return result

    def _por_canal(self) -> bool:
        """Flag DESLIGADA por padrão: ligar exige contrato F4, QA no XPS e rollback."""
        return self.config.get("transcription", {}).get("por_canal") is True

    def _transcrever_por_canal(self, source: Path, bronze: Path, capture_mode: str = "dual",
                              job_file: Path = None, job: dict = None):
        """Um envio por canal, cada um com orçamento, teto e checkpoint próprios.

        O canal já transcrito fica no checkpoint dentro do Bronze: falhar no
        segundo não recobra o primeiro. Gravação mono cai para uma origem
        genérica em vez de alegar microfone e sistema onde só existe um canal.
        """
        from castanha.budget import BudgetManager
        from castanha.transcription import (VpsSshTranscriber, TranscriptionPending,
                                            provider_selection, restore_provider)

        cfg = load_config().get("transcription", {})
        revision = cfg.get("provider_revision", 0)
        if type(revision) is not int or revision < 0:
            raise TranscriptionPending("Revisão de provedor inválida")
        selection_path = bronze / ".providers" / (file_sha256(source) + ".json")
        saved_selection = json.loads(selection_path.read_text()) if selection_path.exists() else None
        recording_transcriber, selection = None, None
        if saved_selection is not None:
            # Validar antes de ler revision; corrupção nunca libera nova seleção.
            restore_provider(saved_selection, require_credentials=False)
            if revision <= saved_selection["revision"]:
                selection = saved_selection
                recording_transcriber = restore_provider(saved_selection)
                revision = selection["revision"]
        checkpoint_root = bronze / ".channels"
        if revision:
            checkpoint_root = checkpoint_root / f"revision-{revision}"
        selected = {}

        class ChannelBudget:
            transcriber = None

            def can_use_groq(self, duration):
                # O teto de duração continua comum; orçamento Groq não veta VPS.
                return isinstance(self.transcriber, VpsSshTranscriber) or budget.can_use_groq(duration)

        budget = BudgetManager()
        channel_budget = ChannelBudget()

        def configuration(entry):
            # Resolver antes do replay: mudar o provedor efetivo também invalida
            # o checkpoint. A seleção não envia áudio e não registra consumo.
            nonlocal recording_transcriber, selection
            if recording_transcriber is None:
                recording_transcriber = get_transcriber(estimated_duration_sec=entry["duration_seconds"])
                selection = provider_selection(recording_transcriber, cfg)
                if selection is not None:
                    if saved_selection is not None:
                        if (selection["selected_provider"] != saved_selection["selected_provider"]
                                and selection.get("fallback_from") != saved_selection["selected_provider"]):
                            raise TranscriptionPending("Troca de provedor exige fallback_from explícito")
                        archive = selection_path.parent / "history" / f"{selection_path.stem}-r{saved_selection['revision']}.json"
                        if not archive.exists():
                            write_json(archive, saved_selection)
                    # Publicar seleção antes de qualquer chamada do provedor.
                    write_json(selection_path, selection)
            transcriber = recording_transcriber
            if selection is not None and job is not None and job_file is not None:
                job.update(selected_provider=selection["selected_provider"],
                           provider_selection=selection, provider_revision=selection["revision"],
                           fallback_from=selection.get("fallback_from"))
                write_json(job_file, job)
            selected[str(entry["path"])] = transcriber
            channel_budget.transcriber = transcriber
            model = getattr(transcriber, "model", None)
            configuration = {"effective_provider": type(transcriber).__name__,
                    "result_provider": {"GroqTranscriber": "groq", "DeepgramTranscriber": "deepgram",
                                        "VpsSshTranscriber": "vps_whisper_large_v3",
                                        "MockTranscriber": "mock"}.get(type(transcriber).__name__),
                    "requested_provider": cfg.get("provider", "groq"),
                    "model": model if isinstance(model, str) else cfg.get("groq_model", "whisper-large-v3-turbo"),
                    "language": cfg.get("language", "auto"),
                    "groq_model": cfg.get("groq_model", "whisper-large-v3-turbo"),
                    "deepgram_model": cfg.get("deepgram_model", "nova-2"),
                    "transcribe_mode": "mic_only",
                    "origin_policy": "capture-mode-v2"}
            if selection is not None:
                configuration = {"effective_provider": type(transcriber).__name__,
                                 "result_provider": configuration["result_provider"],
                                 "selection": selection, "transcribe_mode": "mic_only",
                                 "transport_contract": "flac-mono-v1", "sample_rate": 16000,
                                 "origin_policy": "capture-mode-v2"}
            return configuration

        def transcrever(path: Path, duration_seconds: float):
            # A duração é a DO CANAL: o orçamento é consultado por envio, e o
            # segundo canal já enxerga o que o primeiro consumiu.
            transcriber = selected[str(path)]
            return transcriber.transcribe(path, mode="mic_only")

        return transcribe_dual(source, checkpoint_root, transcrever,
                               pipeline_id="canal-v2", budget=channel_budget,
                               capture_mode=capture_mode, configuration_for_channel=configuration)

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
        def recorded_time(item):
            value = item[1].get("recorded_at")
            try:
                if not isinstance(value, str) or "T" not in value:
                    raise ValueError("timestamp ausente")
                parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
                return (parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed).timestamp(), item[1]["id"]
            except (ValueError, TypeError, OverflowError) as exc:
                raise ValueError("recorded_at inválido; jobs preservados antes do processamento") from exc
        jobs.sort(key=recorded_time)
        if not metadata and jobs:
            first = jobs[0][1]
            state = first["state"]
            metadata = {"slug": slug, "title": (state.get("current_meeting") or {}).get("title") or "Reunião",
                        "recorded_at": first["recorded_at"], "calendar_event": state.get("current_meeting") or {},
                        "mode": state.get("mode", "dual"), "recordings": []}
        from castanha.transcription import TranscriptionPending
        for job_file, job in jobs:
            revision = self.config.get("transcription", {}).get("provider_revision", 0)
            if (self._por_canal() and type(revision) is int
                    and revision > job.get("provider_revision", 0) and job["stage"] != "pending"):
                archive = jobs_dir / "history" / f"{job['id']}-r{job.get('provider_revision', 0)}.json"
                if not archive.exists():
                    write_json(archive, job)
                job.update(stage="pending", transcript="", utterances=[], channels=[])
                write_json(job_file, job)
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
                elif self._por_canal():
                    transcription = self._transcrever_por_canal(source, bronze, capture_mode=mode,
                                                               job_file=job_file, job=job)
                    if not transcription.text.strip():
                        raise RuntimeError("Transcrição vazia; áudio preservado para nova tentativa")
                    job.update(transcript=transcription.text, provider=transcription.provider,
                               utterances=[asdict(segment) for segment in transcription.utterances],
                               channel_provenance=True, capture_mode=mode,
                               channels=transcription.raw_response.get("channels") or [])
                else:
                    from castanha.transcription import provider_selection, restore_provider
                    if job.get("provider_selection"):
                        transcriber = restore_provider(job["provider_selection"])
                    else:
                        transcriber = get_transcriber(estimated_duration_sec=duration or state.get("elapsed_seconds") or 60)
                        selection = provider_selection(transcriber, self.config.get("transcription", {}))
                        if selection is not None:
                            job.update(selected_provider=selection["selected_provider"], provider_selection=selection,
                                       provider_revision=selection["revision"], fallback_from=selection.get("fallback_from"))
                            write_json(job_file, job)
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
                    job.update(transcript=transcription.text, provider=transcription.provider,
                               utterances=[asdict(segment) for segment in transcription.utterances],
                               channel_provenance=transcription.raw_response.get("channel_provenance") is True,
                               channels=transcription.raw_response.get("channels") or [])
                job.update(stage="transcribed", error=None)
                job.pop("pending_reason", None)
            except TranscriptionPending as exc:
                # Um job remoto aceito, ou um transporte temporariamente
                # indisponível, não é falha. O checkpoint continua pendente
                # para que a próxima rodada consulte o mesmo job determinístico
                # sem reenviar o áudio nem mostrar um falso erro.
                job.update(stage="pending", provider="pending", error=None,
                           pending_reason=str(exc))
            except Exception as exc:
                job.update(error=str(exc), provider="failed")
                job.pop("pending_reason", None)
            write_json(job_file, job)

        # Pendência/erro histórico também contam; o último job não pode apagá-los.
        pending_jobs = [job for _, job in jobs
                        if job.get("pending_reason") or
                        (job.get("stage") == "pending" and job.get("provider") == "pending")]
        errors = [f"{job['id']}: a transcrição falhou ({job['error']})" if job.get("error")
                  else f"{job['id']}: transcrição falhou"
                  for _, job in jobs
                  if job.get("error") or
                  (job.get("stage") not in ("transcribed", "done") and job not in pending_jobs)]
        pending_messages = [f"{job['id']}: {job.get('pending_reason') or 'transcrição pendente'}"
                            for job in pending_jobs]

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
            if (job.get("transcript") and job.get("provider") not in (None, "mock", "failed")
                    and job.get("stage") in ("transcribed", "done")):
                parts.append(job["transcript"])
            source = Path(job["audio_path"])
            record = {"id": source.name, "filename": source.name, "path": str(source),
                      "job_id": job["id"], "sha256": job.get("sha256"), "recorded_at": job["recorded_at"],
                      "size_bytes": source.stat().st_size if source.exists() else 0,
                      "duration_seconds": job.get("duration_seconds", 0),
                      "audio_status": job.get("audio_status", "desconhecido"),
                      "transcribed": job.get("stage") in ("transcribed", "done"),
                      "transcription_error": job.get("error"),
                      "transcription_provider": job.get("provider"),
                      "capture_mode": job.get("capture_mode", job.get("state", {}).get("mode", "dual"))}
            if job.get("channel_provenance"):
                # A origem fica junto da gravação no Bronze, para a ponte F4 ler
                # o canal em vez de um nome de convidado que ninguém provou ter falado.
                record["channel_provenance"] = True
                record["origins"] = [{"origin": c.get("origin"), "silent": c.get("silent") is True,
                                      "utterance_count": c.get("utterance_count", 0)}
                                     for c in job.get("channels", [])]
            records.append(record)
        transcript = "\n\n".join(p for p in parts if p)
        atomic_write(bronze / "transcript_raw.txt", transcript)
        # Tempos são relativos à gravação, nunca somados como se pausas não existissem.
        # Histórico sem segmentos continua explicitamente sem atribuição de origem.
        write_json(bronze / "transcript_segments.json", {
            "version": 1,
            "recordings": [
                {"job_id": job["id"], "recorded_at": job["recorded_at"],
                 "source_sha256": job.get("sha256"), "provider": job.get("provider"),
                 "time_reference": "recording_start",
                 "capture_mode": job.get("capture_mode", job.get("state", {}).get("mode", "dual")),
                 "channel_provenance": job.get("channel_provenance") is True,
                 "channels": job.get("channels", []),
                 "utterance_count": len(job.get("utterances", [])),
                 "utterances": job.get("utterances", [])}
                for _, job in jobs if job.get("provider") not in ("mock", "failed")
                and job.get("stage") in ("transcribed", "done")
            ],
        })
        last_job = jobs[-1][1]
        # Evidência de fala real vence silêncio posterior. Falha e pendência
        # ficam separadas da evidência, mesmo quando há texto parcial aproveitável.
        successful_jobs = {job["id"] for _, job in jobs
                           if job.get("stage") in ("transcribed", "done") and job.get("transcript", "").strip()}
        memory_records = [r for r in records
                          if r.get("transcription_provider") not in (None, "mock", "failed", "pending")
                          and not str(r.get("transcription_provider", "")).startswith("nenhum")
                          and (r.get("job_id") in successful_jobs
                               or (not r.get("job_id") and bool(parts and base.exists())))]
        providers = {r["transcription_provider"] for r in memory_records}
        provider = ((next(iter(providers)) if len(providers) == 1 else "mixed") if providers
                    else "pending" if pending_jobs else "failed" if errors else "mock" if any(r.get("transcription_provider") == "mock" for r in records)
                    else "nenhum (áudio em silêncio)")
        statuses = {r.get("audio_status") for r in memory_records}
        audio_status = ("ok" if "ok" in statuses else "mic_mudo" if "mic_mudo" in statuses
                        else "desconhecido" if memory_records or errors
                        else "sem_audio" if all(r.get("audio_status") == "sem_audio" for r in records)
                        else "desconhecido")
        transcription_error = "; ".join(errors) or None
        metadata.pop("summary_status", None)
        metadata.pop("summary_error", None)
        metadata.update(recordings=records, recordings_count=len(records),
                        duration_seconds=sum(r.get("duration_seconds", 0) for r in records),
                        bronze_audio_file=last_job["audio_path"], transcription_provider=provider,
                        memory_recording_ids=[r["id"] for r in memory_records],
                        transcription_error=transcription_error, audio_status=audio_status,
                        transcription_pending=bool(pending_jobs),
                        transcription_pending_reason="; ".join(pending_messages) or None,
                        audio_diagnostico=audio_status_message(audio_status),
                        audio_levels=last_job.get("audio_levels", []),
                        processing_status="pending" if errors or pending_jobs else "complete")
        write_json(bronze / "metadata.json", metadata)
        if (errors and self._por_canal()) or pending_jobs:
            # Canal recusado não pode gerar Silver/Gold parcial nem entrega nova.
            return {"status": "partial", "result": {
                "slug": slug, "title": metadata["title"], "bronze_dir": str(bronze),
                "silver_file": str(self.storage.silver_dir / f"{slug}.md"),
                "gold_file": str(self.storage.gold_dir / f"{slug}.json"),
                "audio_status": audio_status, "audio_diagnostico": metadata["audio_diagnostico"],
                "transcription_provider": provider, "transcription_error": transcription_error,
                "transcription_pending": bool(pending_jobs),
                "transcription_pending_reason": metadata.get("transcription_pending_reason"),
                "zinom": metadata.get("zinom") or {"status": "pending"},
                "problemas": errors + pending_messages}}
        from castanha.summarizer import LlmUnavailable
        try:
            silver_content = self.summarizer.generate_silver(metadata, transcript)
            silver_provider = self.summarizer.last_provider  # o Gold pode cair na reserva
            gold_data = self.summarizer.generate_gold(metadata, silver_content, transcript)
        except LlmUnavailable as exc:
            # Cota, rede ou provedor: a transcrição já está nos checkpoints e nada
            # vai ao Zinom. O resumo fica pendente e a retomada refaz só ele.
            metadata.update(processing_status="pending", summary_status="pending", summary_error=str(exc))
            write_json(bronze / "metadata.json", metadata)
            return {"status": "partial", "result": {
                "slug": slug, "title": metadata["title"], "bronze_dir": str(bronze),
                "silver_file": str(self.storage.silver_dir / f"{slug}.md"),
                "gold_file": str(self.storage.gold_dir / f"{slug}.json"),
                "audio_status": audio_status, "audio_diagnostico": metadata["audio_diagnostico"],
                "transcription_provider": provider, "transcription_error": transcription_error,
                "summary_status": "pending", "summary_error": str(exc),
                # O recibo antigo (se houver) fica no metadata; o resultado desta
                # rodada é pendente, senão a fila zera o backoff e o CLI diz "salvo".
                "zinom": {"status": "pending", "reason": t("engine.problem_summary_pending", exc=exc)},
                "problemas": errors + [t("engine.problem_summary_pending", exc=exc)]}}
        # Quem resumiu fica no Bronze antes do recibo: record_zinom_result relê o disco.
        metadata["summary_provider"] = silver_provider
        write_json(bronze / "metadata.json", metadata)
        silver_path = self.storage.save_silver(slug, silver_content)
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
            errors.append(audio_status_message(audio_status))
        if any(r.get("transcription_provider") == "mock" for r in records):
            errors.append(t("engine.problem_mock_transcription"))
        if zinom_status.get("facts_status") == "pending_lineage":
            errors.append(zinom_status["reason"])
        if zinom_status.get("status") == "error":
            errors.extend(zinom_status.get("errors", []))
        summary = {"slug": slug, "title": metadata["title"], "bronze_dir": str(bronze),
                   "silver_file": str(silver_path), "gold_file": str(gold_path),
                   "audio_status": audio_status, "audio_diagnostico": metadata["audio_diagnostico"],
                   "transcription_provider": provider, "transcription_error": transcription_error,
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
                    last_res["audio_diagnostico"] = t("audio_status.audio_apagado")
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
                self.state_mgr.write({"status": "idle", "pid": None, "processing_pid": None, "audio_path": None,
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
        # Fixar a origem ANTES de promover o provider agregado. O legado não
        # separava trechos por gravação: havendo mock, isolar o agregado inteiro
        # é a única opção que não atribui texto simulado a uma fonte real.
        for rec in meta.get("recordings", []):
            rec.setdefault("transcription_provider", "pending" if rec.get("transcribed") is False
                           else meta.get("transcription_provider"))
        has_mock = (meta.get("transcription_provider") == "mock" or any(
            r.get("transcription_provider") == "mock" for r in meta.get("recordings", [])))
        isolated = has_mock and not meta.get("legacy_mock_isolated")
        if isolated:
            archive = bronze_dir / ".mock-history"
            archive.mkdir(exist_ok=True)
            # Cada publicação é atômica. Se morrer no meio, o retry conserva
            # o primeiro snapshot e repete a limpeza antes de transcrever.
            originals = [(bronze_dir / "metadata.json", archive / "metadata.json"),
                         (bronze_dir / "transcript_raw.txt", archive / "transcript_raw.txt"),
                         (self.storage.silver_dir / f"{slug}.md", archive / "silver.md"),
                         (self.storage.gold_dir / f"{slug}.json", archive / "gold.json")]
            for source, target in originals:
                if source.exists() and not target.exists():
                    atomic_write(target, source)
            atomic_write(bronze_dir / "transcript_raw.txt", "")
            # Sync não pode ler notas simuladas se houver morte depois de
            # promover o provider, antes da geração das notas reais.
            atomic_write(self.storage.silver_dir / f"{slug}.md", "")
            write_json(self.storage.gold_dir / f"{slug}.json", {})
        meta["legacy_mock_isolated"] = True
        self.storage.write_bronze_metadata(slug, meta)
        origins = {r.get("filename"): r.get("transcription_provider") for r in meta.get("recordings", [])}
        # Sem marca (reunião antiga): se já há texto, ela já foi transcrita.
        pendentes = [
            r for r in recordings
            if origins.get(r["filename"]) != "mock" and (
                r.get("transcribed") is False or (r.get("transcribed") is None and not texto_antes.strip()))
        ]

        notify(t("notify.reprocessing_title"), t("notify.reprocessing_body", title=title))

        erros: List[str] = []
        pendencias: List[str] = []
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
                meta["audio_diagnostico"] = audio_status_message(audio_status)
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
                                              transcription_provider="nenhum (áudio em silêncio)",
                                              duration_seconds=dur)
                provider_final = provider_final or "nenhum (áudio em silêncio)"
                continue

            if self._por_canal():
                # O "tentar de novo" não pode virar a exceção que manda o estéreo
                # inteiro para um Whisper só. Aqui o texto já vem com a origem em
                # cada linha; os segmentos ficam no checkpoint por canal.
                try:
                    resultado = self._transcrever_por_canal(audio_path, self.storage.bronze_dir / slug, capture_mode=mode)
                    texto, provider, erro = resultado.text, resultado.provider, None
                except TranscriptionPending as exc:
                    texto, provider, erro = "", "pending", str(exc)
                except Exception as exc:
                    texto, provider, erro = "", "failed", str(exc)
            else:
                texto, provider, erro = self._transcribe_with_fallback(audio_path, mode, dur or 60.0)
            if texto.strip():
                if provider == "mock":
                    # O checkpoint conserva o texto e sua gravação, fora do
                    # agregado que alimenta Silver/Gold e memória.
                    write_json(bronze_dir / ".mock-history" / f"{rec['filename']}.json",
                               {"recording": rec, "provider": provider, "transcript": texto})
                    erros.append("Transcrição simulada preservada separadamente, não enviada à memória")
                else:
                    self.storage.append_transcript(slug, rec["filename"], texto)
                self.storage.update_recording(slug, rec["filename"], transcribed=True,
                                              transcription_error=None, transcription_pending=False,
                                              transcription_pending_reason=None, audio_status=audio_status,
                                              transcription_provider=provider,
                                              duration_seconds=dur)
                if provider != "mock":
                    provider_final = provider
                    novo_texto = True
            else:
                motivo = erro or "transcrição vazia"
                if provider == "pending":
                    self.storage.update_recording(
                        slug, rec["filename"], transcribed=False,
                        transcription_error=None, transcription_pending=True,
                        transcription_pending_reason=motivo, audio_status=audio_status,
                        transcription_provider="pending", duration_seconds=dur)
                    pendencias.append(f"{rec['filename']}: {motivo}")
                else:
                    self.storage.update_recording(
                        slug, rec["filename"], transcribed=False,
                        transcription_error=motivo, audio_status=audio_status,
                        transcription_provider=provider, duration_seconds=dur)
                    erros.append(f"{rec['filename']}: {motivo}")

        meta = self.storage._read_bronze_metadata(slug)
        transcript = self.storage.read_transcript(slug)
        recordings = self.storage.list_meeting_recordings(slug)
        soma = sum(float(r.get("duration_seconds") or 0) for r in recordings)
        if soma > 0:
            meta["duration_seconds"] = soma
        if pendencias and not transcript.strip():
            meta["transcription_provider"] = "pending"
        elif erros and not transcript.strip():
            meta["transcription_provider"] = "failed"
        elif provider_final:
            meta["transcription_provider"] = provider_final
        meta["memory_recording_ids"] = [r.get("id") or r["filename"] for r in meta.get("recordings", [])
                                        if r.get("transcription_provider") not in ("mock", "failed", "pending")
                                        and r.get("audio_status") != "sem_audio"]
        meta["transcription_error"] = "; ".join(erros) if erros else None
        meta["transcription_pending"] = bool(pendencias)
        meta["transcription_pending_reason"] = "; ".join(pendencias) or None
        self.storage.write_bronze_metadata(slug, meta)

        silver_path = self.storage.silver_dir / f"{slug}.md"
        gold_path = self.storage.gold_dir / f"{slug}.json"
        zinom_status: Dict[str, Any] = meta.get("zinom") or {}
        # Nota nova só com texto novo, ou quando nada estava pendente (aí o
        # pedido é refazer as notas). Falhar de novo não mexe na nota que existe.
        refazer_notas = transcript.strip() and (novo_texto or not pendentes)
        if refazer_notas:
            # 4. Silver, 5. Gold, 6. Zinom (editando a nota anterior, se houver)
            from castanha.summarizer import LlmUnavailable
            meta.pop("summary_status", None)
            meta.pop("summary_error", None)
            try:
                silver_content = self.summarizer.generate_silver(meta, transcript)
                silver_provider = self.summarizer.last_provider
                gold_data = self.summarizer.generate_gold(meta, silver_content, transcript)
            except LlmUnavailable as exc:
                # Transcrição já está no Bronze; a nota antiga (se houver) fica como está.
                meta.update(summary_status="pending", summary_error=str(exc))
                self.storage.write_bronze_metadata(slug, meta)
                return {"status": "partial", "result": {
                    "slug": slug, "title": title, "bronze_dir": str(bronze_dir),
                    "silver_file": str(silver_path), "gold_file": str(gold_path),
                    "audio_status": meta.get("audio_status", "ok"),
                    "audio_diagnostico": meta.get("audio_diagnostico", ""),
                    "transcription_provider": meta.get("transcription_provider") or "failed",
                    "transcription_error": meta.get("transcription_error"),
                    "summary_status": "pending", "summary_error": str(exc),
                    "zinom": {"status": "pending", "reason": t("engine.problem_summary_pending", exc=exc)},
                    "problemas": [t("engine.problem_summary_pending", exc=exc)]}}
            meta["summary_provider"] = silver_provider
            self.storage.write_bronze_metadata(slug, meta)
            silver_path = self.storage.save_silver(slug, silver_content)
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
            "transcription_pending": bool(pendencias),
            "transcription_pending_reason": meta.get("transcription_pending_reason"),
            "zinom": zinom_status,
        }

        # Atualiza o state caso seja a última reunião
        state = self.state_mgr.read()
        last_res = state.get("last_result")
        if last_res and last_res.get("slug") == slug:
            self.state_mgr.write({"last_result": result_summary})

        problemas = []
        if erros:
            problemas.append(t("engine.problem_transcription_failed", errors="; ".join(erros)))
            notify(t("notify.reprocess_fail_title"), t("notify.reprocess_fail_body", title=title), timeout=10000)
        elif pendencias:
            problemas.append(t("engine.problem_transcription_pending", errors="; ".join(pendencias)))
        else:
            notify(t("notify.reprocessed_title"), t("notify.reprocessed_body", title=title))

        if meta.get("audio_status") in ("sem_audio", "mic_mudo"):
            problemas.append(audio_status_message(meta["audio_status"]) or meta["audio_status"])
        if isinstance(zinom_status, dict) and zinom_status.get("status") == "error":
            problemas.extend(zinom_status.get("errors", []))
        result_summary["problemas"] = problemas

        return {"status": "partial" if problemas else "success", "result": result_summary}
