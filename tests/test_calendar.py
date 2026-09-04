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
