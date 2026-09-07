import datetime
import unittest
from castanha.calendar import parse_ics_content, extract_conference_url

SAMPLE_ICS = r"""BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//Google Inc//Google Calendar 70.9054//EN
BEGIN:VEVENT
DTSTART:20260904T130000Z
DTEND:20260904T140000Z
UID:meeting-abc-123@google.com
SUMMARY:Alinhamento Estratégico Zinom
DESCRIPTION:Pauta da reunião e alinhamentos:\nhttps://meet.google.com/xyz-uvwx-rst\nFavor participar.
LOCATION:Google Meet
ATTENDEE;CUTYPE=INDIVIDUAL;ROLE=REQ-PARTICIPANT;CN=Bruno Moniz:mailto:bruno@example.com
ATTENDEE;CUTYPE=INDIVIDUAL;ROLE=REQ-PARTICIPANT;CN=Dev Lead:mailto:dev@example.com
END:VEVENT
END:VCALENDAR"""

class TestCalendar(unittest.TestCase):
    def test_extract_conference_url(self):
        url = extract_conference_url("Link da chamada: https://meet.google.com/abc-defg-hij venham todos")
        self.assertEqual(url, "https://meet.google.com/abc-defg-hij")

        zoom = extract_conference_url("Entrar via Zoom: https://us02web.zoom.us/j/123456789 sala de espera")
        self.assertEqual(zoom, "https://us02web.zoom.us/j/123456789")

    def test_parse_ics_content(self):
        events = parse_ics_content(SAMPLE_ICS)
        self.assertEqual(len(events), 1)
        event = events[0]
        self.assertEqual(event.uid, "meeting-abc-123@google.com")
        self.assertEqual(event.title, "Alinhamento Estratégico Zinom")
        self.assertEqual(event.conference_url, "https://meet.google.com/xyz-uvwx-rst")
        self.assertEqual(len(event.attendees), 2)
        self.assertEqual(event.attendees[0].name, "Bruno Moniz")
        self.assertEqual(event.attendees[0].email, "bruno@example.com")
        self.assertEqual(event.attendees[1].name, "Dev Lead")

if __name__ == "__main__":
    unittest.main()


class TestIcalDiaInteiro(unittest.TestCase):
    def test_value_date_vira_dia_inteiro_local(self):
        import datetime
        from castanha.calendar import parse_ics_content
        ics = "BEGIN:VCALENDAR\nBEGIN:VEVENT\nUID:x1\nSUMMARY:Aniversário\nDTSTART;VALUE=DATE:20260905\nDTEND;VALUE=DATE:20260906\nEND:VEVENT\nEND:VCALENDAR\n"
        (ev,) = parse_ics_content(ics)
        self.assertTrue(ev.all_day)
        self.assertEqual((ev.start.day, ev.start.hour), (5, 0))
        self.assertEqual(ev.start.tzinfo, datetime.datetime.now().astimezone().tzinfo)
        self.assertEqual(ev.end - ev.start, datetime.timedelta(days=1))

    def test_com_hora_continua_com_hora(self):
        from castanha.calendar import parse_ics_content
        ics = "BEGIN:VCALENDAR\nBEGIN:VEVENT\nUID:x2\nSUMMARY:Reunião\nDTSTART:20260905T130000Z\nDTEND:20260905T140000Z\nEND:VEVENT\nEND:VCALENDAR\n"
        (ev,) = parse_ics_content(ics)
        self.assertFalse(ev.all_day)


class TestZinomCalendarFalhas(unittest.TestCase):
    """Agenda honesta: falha numa agenda mantém a última lista boa e avisa.

    Antes, um rate limit do hub (60/min, compartilhado com o resto da máquina)
    virava `[]` sem aviso, e o painel dizia "nada nas próximas horas".
    """

    AUSENTE = "list_event_details devolveu erro: MCP error -32602: Tool list_event_details not found"
    GOOGLE_404 = ("list_event_details devolveu erro: Google Calendar 404: "
                  "https://www.googleapis.com/calendar/v3/calendars/x/events Not Found")

    def _fonte(self):
        import time
        from castanha.zinom_calendar import ZinomCalendar
        fonte = ZinomCalendar({"zinom": {"token": "t"}, "calendar": {"zinom": {}}})
        fonte._calendars = [
            {"calendar_ref": "a", "summary": "Bruno", "email": "a@x", "primary": True},
            {"calendar_ref": "b", "summary": "Eventos Nora", "email": "a@x", "accessRole": "owner"},
        ]
        fonte._calendars_at = time.time()
        return fonte

    @staticmethod
    def _evento(ref):
        agora = datetime.datetime.now().astimezone()
        return {"id": f"ev-{ref}", "summary": f"Reunião {ref}",
                "start": {"dateTime": (agora + datetime.timedelta(hours=1)).isoformat()},
                "end": {"dateTime": (agora + datetime.timedelta(hours=2)).isoformat()}}

    def _respostas(self, fonte, falhas=None, chamadas=None):
        """`falhas`: {(tool, ref): exceção}. `chamadas` recebe (tool, ref)."""
        falhas = falhas or {}
        agendas = list(fonte._calendars)

        def _call(name, args):
            if name == "list_calendars":
                return {"calendars": agendas}
            ref = args.get("calendar_ref")
            if chamadas is not None:
                chamadas.append((name, ref))
            erro = falhas.get((name, ref))
            if erro is not None:
                raise erro
            return {"events": [self._evento(ref)]}

        fonte._call = _call

    def test_falha_numa_agenda_mantem_a_ultima_lista_boa_e_avisa(self):
        from castanha.zinom_adapter import ZinomError
        fonte = self._fonte()
        self._respostas(fonte)
        boa = fonte.upcoming()
        self.assertEqual([e.uid for e in boa], ["ev-a", "ev-b"])
        self.assertIsNone(fonte.last_error)
        bom_em = fonte.stale_since
        self.assertIsNotNone(bom_em)

        self._respostas(fonte, {("list_event_details", "b"): ZinomError(self.GOOGLE_404, tool="list_event_details")})
        de_novo = fonte.upcoming(force=True)
        self.assertEqual([e.uid for e in de_novo], ["ev-a", "ev-b"])  # não virou só "a"
        self.assertEqual(fonte.last_error, "Google Calendar 404: Not Found")  # sem URL
        self.assertEqual(fonte.stale_since, bom_em)

    def test_sem_ciclo_bom_fica_vazio_com_erro_e_sem_hora(self):
        from castanha.zinom_adapter import ZinomError
        from castanha.zinom_calendar import MOTIVO_LIMITE
        fonte = self._fonte()
        chamadas = []
        self._respostas(fonte, {("list_event_details", "a"): ZinomError("HTTP 429: Too Many Requests")}, chamadas)
        self.assertEqual(fonte.upcoming(), [])
        self.assertEqual(fonte.last_error, MOTIVO_LIMITE)
        self.assertIsNone(fonte.stale_since)
        # Estourou o limite: não bate nas outras agendas no mesmo ciclo.
        self.assertEqual(chamadas, [("list_event_details", "a")])

    def test_ciclo_bom_depois_da_falha_limpa_o_aviso(self):
        from castanha.zinom_adapter import ZinomError
        fonte = self._fonte()
        self._respostas(fonte, {("list_event_details", "a"): ZinomError("HTTP 429: Too Many Requests")})
        fonte.upcoming()
        self._respostas(fonte)
        self.assertEqual(len(fonte.upcoming(force=True)), 2)
        self.assertIsNone(fonte.last_error)

    def test_falha_na_lista_de_agendas_nao_levanta_e_diz_sem_conexao(self):
        from castanha.zinom_adapter import ZinomError
        from castanha.zinom_calendar import MOTIVO_CONEXAO
        fonte = self._fonte()
        self._respostas(fonte)
        fonte.upcoming()
        fonte._calendars_at = 0.0  # a lista de agendas expirou

        def _call(name, args):
            raise ZinomError("<urlopen error [Errno 111] Connection refused>")
        fonte._call = _call
        self.assertEqual([e.uid for e in fonte.upcoming(force=True)], ["ev-a", "ev-b"])
        self.assertEqual(fonte.last_error, MOTIVO_CONEXAO)

    def test_falha_nao_e_rebatida_a_cada_minuto(self):
        """Mesma cadência com falha: o daemon pergunta a cada 60 s, a fonte só vai ao hub a cada 300 s."""
        from castanha.zinom_adapter import ZinomError
        fonte = self._fonte()
        chamadas = []
        self._respostas(fonte, {("list_event_details", "a"): ZinomError("HTTP 429: Too Many Requests")}, chamadas)
        fonte.upcoming()
        fonte.upcoming()
        self.assertEqual(len(chamadas), 1)

    # ---- tool ausente só pela forma JSON-RPC do hub
    def test_tool_ausente_rebaixa_para_a_listagem_magra(self):
        from castanha.zinom_adapter import ZinomError
        fonte = self._fonte()
        chamadas = []
        self._respostas(fonte, {("list_event_details", "a"): ZinomError(self.AUSENTE, tool="list_event_details")}, chamadas)
        self.assertEqual([e.uid for e in fonte.upcoming()], ["ev-a", "ev-b"])
        self.assertIsNone(fonte.last_error)
        self.assertFalse(fonte._detalhe_disponivel)
        self.assertEqual(chamadas, [("list_event_details", "a"), ("list_events", "a"), ("list_events", "b")])

    def test_404_do_google_numa_agenda_nao_rebaixa_as_outras(self):
        from castanha.zinom_adapter import ZinomError
        fonte = self._fonte()
        chamadas = []
        self._respostas(fonte, {("list_event_details", "a"): ZinomError(self.GOOGLE_404, tool="list_event_details")}, chamadas)
        self.assertEqual(fonte.upcoming(), [])  # nunca houve ciclo bom
        self.assertNotEqual(fonte._detalhe_disponivel, False)
        self.assertEqual(chamadas, [("list_event_details", "a"), ("list_event_details", "b")])
        self.assertIn("404", fonte.last_error)

    def test_tool_ausente_com_outro_nome_nao_rebaixa(self):
        from castanha.zinom_adapter import ZinomError
        fonte = self._fonte()
        chamadas = []
        outra = "list_event_details devolveu erro: MCP error -32602: Tool list_events not found"
        self._respostas(fonte, {("list_event_details", "a"): ZinomError(outra, tool="list_event_details")}, chamadas)
        fonte.upcoming()
        self.assertNotEqual(fonte._detalhe_disponivel, False)
        self.assertNotIn(("list_events", "a"), chamadas)
        self.assertIsNotNone(fonte.last_error)

    def test_motivo_curto_fala_a_lingua_do_painel(self):
        from castanha.zinom_adapter import ZinomError
        from castanha.zinom_calendar import MOTIVO_CONEXAO, MOTIVO_LIMITE, motivo_curto
        self.assertEqual(motivo_curto(ZinomError("HTTP 429: Rate limit exceeded")), MOTIVO_LIMITE)
        self.assertEqual(motivo_curto(ZinomError("tools/call: rate limit exceeded for client")), MOTIVO_LIMITE)
        self.assertEqual(motivo_curto(ZinomError("<urlopen error [Errno -3] Temporary failure in name resolution>")), MOTIVO_CONEXAO)
        self.assertEqual(motivo_curto(ZinomError("The read operation timed out")), MOTIVO_CONEXAO)
        self.assertEqual(motivo_curto(ZinomError("HTTP 502: Bad Gateway")), MOTIVO_CONEXAO)
        self.assertEqual(motivo_curto(ZinomError("tools/call: Unauthorized")), "Unauthorized")
        longo = motivo_curto(ZinomError("list_event_details devolveu erro: " + "x" * 200))
        self.assertLessEqual(len(longo), 80)
        self.assertNotIn("http", motivo_curto(ZinomError(self.GOOGLE_404)))
