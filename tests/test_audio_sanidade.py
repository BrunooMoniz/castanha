"""Detecção de gravação muda.

Caso de regressão real: em 2026-09-04 o Bruno gravou 35s com o microfone mudo
no teclado da Dell. O canal do mic saiu em silêncio digital, o Whisper alucinou
"Thank you. Thank you." e o Castanha anunciou "Notas Prontas!".
"""

import subprocess
import tempfile
import unittest
from pathlib import Path

from castanha.audio import (
    ChannelLevels,
    classify_audio,
    is_default_source_muted,
    is_silent,
    measure_channel_levels,
    probe_duration_seconds,
)


def _ch(index, label, mean_db, max_db):
    return ChannelLevels(
        channel=index, label=label, mean_db=mean_db, max_db=max_db,
        silent=is_silent(mean_db, max_db),
    )


class TestClassificacao(unittest.TestCase):
    def test_mic_mudo_canal_do_sistema_com_som(self):
        # Níveis medidos na gravação real das 10:31 (mic mudo no teclado).
        levels = [_ch(0, "microfone", -91.0, -91.0), _ch(1, "sistema", -48.5, -10.6)]
        self.assertEqual(classify_audio(levels), "mic_mudo")

    def test_gravacao_boa(self):
        # Níveis medidos na gravação real das 10:42, com o mic desmutado.
        levels = [_ch(0, "microfone", -34.4, -14.1), _ch(1, "sistema", -91.0, -91.0)]
        self.assertEqual(classify_audio(levels), "ok")

    def test_tudo_mudo(self):
        levels = [_ch(0, "microfone", -91.0, -91.0), _ch(1, "sistema", -91.0, -91.0)]
        self.assertEqual(classify_audio(levels), "sem_audio")

    def test_vazamento_do_opus_nao_conta_como_som(self):
        # Canal em silêncio absoluto ao lado de um canal em 0 dBFS: o encoder
        # deixa o pico subir para -38 dB, mas a média entrega que não há sinal.
        levels = [_ch(0, "microfone", -73.3, -38.4), _ch(1, "sistema", -3.0, -0.5)]
        self.assertEqual(classify_audio(levels), "mic_mudo")

    def test_fala_curta_em_reuniao_longa_nao_e_silencio(self):
        # Média baixíssima porque quase tudo é pausa, mas o pico é de fala.
        levels = [_ch(0, "microfone", -68.0, -18.0), _ch(1, "sistema", -30.0, -12.0)]
        self.assertEqual(classify_audio(levels), "ok")

    def test_sem_medicao(self):
        self.assertEqual(classify_audio([]), "desconhecido")

    def test_mic_only_com_som(self):
        self.assertEqual(classify_audio([_ch(0, "microfone", -30.0, -20.0)]), "ok")

    def test_mic_only_mudo(self):
        self.assertEqual(classify_audio([_ch(0, "microfone", -91.0, -91.0)]), "sem_audio")


def _gerar_ogg(path: Path, esquerda: str, direita: str, segundos: float = 2.0) -> bool:
    """Gera um ogg estéreo com as fontes pedidas ('anullsrc' ou um tom)."""
    cmd = [
        "ffmpeg", "-hide_banner", "-nostdin", "-y",
        "-f", "lavfi", "-t", str(segundos), "-i", esquerda,
        "-f", "lavfi", "-t", str(segundos), "-i", direita,
        "-filter_complex", "[0:a][1:a]amerge=inputs=2[a]",
        "-map", "[a]", "-c:a", "libopus", "-b:a", "32k", str(path),
    ]
    return subprocess.run(cmd, capture_output=True).returncode == 0


SILENCIO = "anullsrc=r=48000:cl=mono"
TOM = "sine=frequency=440:sample_rate=48000"


@unittest.skipUnless(
    subprocess.run(["which", "ffmpeg"], capture_output=True).returncode == 0,
    "ffmpeg não instalado",
)
class TestMedicaoReal(unittest.TestCase):
    """Mede arquivos de verdade, gerados na hora: o bug morava no ffmpeg, não no mock."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def test_detecta_canal_esquerdo_mudo(self):
        f = self.tmp / "mic_mudo.ogg"
        if not _gerar_ogg(f, SILENCIO, TOM):
            self.skipTest("ffmpeg não gerou o fixture")
        levels = measure_channel_levels(f, mode="dual")
        self.assertEqual(len(levels), 2)
        self.assertTrue(levels[0].silent, f"canal do mic deveria ser silêncio: {levels[0]}")
        self.assertFalse(levels[1].silent, f"canal do sistema tem tom: {levels[1]}")
        self.assertEqual(classify_audio(levels), "mic_mudo")

    def test_detecta_gravacao_boa(self):
        f = self.tmp / "ok.ogg"
        if not _gerar_ogg(f, TOM, TOM):
            self.skipTest("ffmpeg não gerou o fixture")
        self.assertEqual(classify_audio(measure_channel_levels(f, mode="dual")), "ok")

    def test_duracao_vem_do_arquivo(self):
        f = self.tmp / "dur.ogg"
        if not _gerar_ogg(f, TOM, TOM, segundos=3.0):
            self.skipTest("ffmpeg não gerou o fixture")
        self.assertAlmostEqual(probe_duration_seconds(f), 3.0, delta=0.3)

    def test_arquivo_invalido_nao_explode(self):
        f = self.tmp / "quebrado.ogg"
        f.write_bytes(b"isto nao e audio")
        self.assertEqual(measure_channel_levels(f, mode="dual"), [])
        self.assertIsNone(probe_duration_seconds(f))


class TestMuteDaFonte(unittest.TestCase):
    def test_devolve_bool_ou_none(self):
        self.assertIn(is_default_source_muted(), (True, False, None))


if __name__ == "__main__":
    unittest.main()
