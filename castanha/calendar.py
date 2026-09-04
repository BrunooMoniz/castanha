"""Módulo de integração com calendários (Google Calendar via iCal e feeds padrão)."""

import datetime
import re
import urllib.request
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional
from zoneinfo import ZoneInfo

@dataclass
class Attendee:
    name: str
    email: str

@dataclass
class MeetingEvent:
    uid: str
    title: str
    start: datetime.datetime
    end: datetime.datetime
    attendees: List[Attendee]
    organizer: Optional[str] = None
    conference_url: Optional[str] = None
    description: Optional[str] = None
    location: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        data = asdict(self)
        data["start"] = self.start.isoformat()
        data["end"] = self.end.isoformat()
        return data

CONFERENCE_PATTERNS = [
    re.compile(r"https?://meet\.google\.com/[a-z0-9-]+"),
    re.compile(r"https?://[a-zA-Z0-9.-]+\.zoom\.us/j/[0-9]+[^\s<>\"']*"),
    re.compile(r"https?://teams\.microsoft\.com/l/meetup-join/[^\s<>\"']*"),
    re.compile(r"https?://teams\.live\.com/meet/[^\s<>\"']*"),
    re.compile(r"https?://whereby\.com/[a-zA-Z0-9.-]+"),
]

def extract_conference_url(text: str) -> Optional[str]:
    if not text:
        return None
    for pattern in CONFERENCE_PATTERNS:
        match = pattern.search(text)
        if match:
            return match.group(0)
    return None

def parse_ical_datetime(dt_str: str) -> datetime.datetime:
    dt_str = dt_str.strip()
    tz = datetime.timezone.utc
    if ":" in dt_str:
        prefix, val = dt_str.split(":", 1)
        if "TZID=" in prefix:
            tz_name = prefix.split("TZID=")[1].split(";")[0]
            try:
                tz = ZoneInfo(tz_name)
            except Exception:
                tz = datetime.timezone.utc
        dt_str = val

    if dt_str.endswith("Z"):
        dt_str = dt_str[:-1]
        tz = datetime.timezone.utc

    if len(dt_str) == 8:  # YYYYMMDD
        dt = datetime.datetime.strptime(dt_str, "%Y%m%d")
        return dt.replace(tzinfo=tz)

    dt = datetime.datetime.strptime(dt_str, "%Y%m%dT%H%M%S")
    return dt.replace(tzinfo=tz)

KNOWN_KEYS = {
    "BEGIN", "END", "UID", "SUMMARY", "DESCRIPTION", "LOCATION",
    "DTSTART", "DTEND", "ATTENDEE", "ORGANIZER", "STATUS", "SEQUENCE",
    "CLASS", "CREATED", "LAST-MODIFIED", "TRANSP", "RRULE"
}

def parse_ics_content(ics_text: str) -> List[MeetingEvent]:
    events: List[MeetingEvent] = []
    unfolded_lines: List[str] = []
    for line in ics_text.splitlines():
        if line.startswith((" ", "\t")) and unfolded_lines:
            unfolded_lines[-1] += line[1:]
        else:
            unfolded_lines.append(line.strip())

    in_event = False
    current_data: Dict[str, Any] = {}
    current_attendees: List[Attendee] = []

    for line in unfolded_lines:
        if line == "BEGIN:VEVENT":
            in_event = True
            current_data = {}
            current_attendees = []
            continue
        elif line == "END:VEVENT":
            if in_event and "DTSTART" in current_data and "SUMMARY" in current_data:
                try:
                    start_dt = parse_ical_datetime(current_data["DTSTART"])
                    end_dt = parse_ical_datetime(current_data.get("DTEND", current_data["DTSTART"]))

                    full_text = f"{current_data.get('DESCRIPTION', '')} {current_data.get('LOCATION', '')}"
                    conf_url = extract_conference_url(full_text)

                    event = MeetingEvent(
                        uid=current_data.get("UID", f"evt-{len(events)}"),
                        title=current_data.get("SUMMARY", "Reunião"),
                        start=start_dt,
                        end=end_dt,
                        attendees=list(current_attendees),
                        organizer=current_data.get("ORGANIZER"),
                        conference_url=conf_url,
                        description=current_data.get("DESCRIPTION"),
                        location=current_data.get("LOCATION"),
                    )
                    events.append(event)
                except Exception:
                    pass
            in_event = False
            continue

        if in_event and ":" in line:
            key_part, val = line.split(":", 1)
            key_name = key_part.split(";")[0].upper()

            if key_name == "ATTENDEE":
                name = ""
                cn_match = re.search(r'CN="?([^;":]+)"?', key_part)
                if cn_match:
                    name = cn_match.group(1)
                email = val.replace("mailto:", "").strip()
                if not name:
                    name = email.split("@")[0]
                current_attendees.append(Attendee(name=name, email=email))
            elif key_name == "ORGANIZER":
                current_data["ORGANIZER"] = val.replace("mailto:", "").strip()
            elif key_name in ["UID", "SUMMARY", "LOCATION"]:
                current_data[key_name] = val.replace(r"\n", "\n").replace(r"\,", ",")
            elif key_name == "DESCRIPTION":
                cleaned_desc = val.replace(r"\n", "\n").replace(r"\,", ",")
                current_data["DESCRIPTION"] = cleaned_desc
            elif key_name in ["DTSTART", "DTEND"]:
                current_data[key_name] = line
            else:
                # Se for linha de continuação ou chave customizada, procura link de conferência
                conf = extract_conference_url(line)
                if conf and "DESCRIPTION" in current_data:
                    current_data["DESCRIPTION"] += f"\n{conf}"
        elif in_event and current_data.get("DESCRIPTION"):
            current_data["DESCRIPTION"] += f"\n{line}"

    return events

def fetch_feed_events(feed_url: str) -> List[MeetingEvent]:
    try:
        if feed_url.startswith(("http://", "https://")):
            req = urllib.request.Request(feed_url, headers={"User-Agent": "Castanha-Omarchy/0.1.0"})
            with urllib.request.urlopen(req, timeout=10) as resp:
                text = resp.read().decode("utf-8", errors="replace")
        else:
            path = Path(feed_url).expanduser()
            if not path.exists():
                return []
            text = path.read_text(encoding="utf-8", errors="replace")
        return parse_ics_content(text)
    except Exception as e:
        print(f"[Castanha] Erro ao buscar feed de calendário {feed_url}: {e}")
        return []

def get_upcoming_meetings(feeds: List[Dict[str, str]], window_minutes: int = 120) -> List[MeetingEvent]:
    now = datetime.datetime.now(datetime.timezone.utc)
    all_events: List[MeetingEvent] = []

    for feed in feeds:
        url = feed.get("url", "")
        if url:
            all_events.extend(fetch_feed_events(url))

    cutoff_past = now - datetime.timedelta(minutes=15)
    cutoff_future = now + datetime.timedelta(minutes=window_minutes)

    upcoming = [
        e for e in all_events
        if cutoff_past <= e.start <= cutoff_future
    ]
    upcoming.sort(key=lambda x: x.start)
    return upcoming

def get_next_meeting(feeds: List[Dict[str, str]]) -> Optional[MeetingEvent]:
    upcoming = get_upcoming_meetings(feeds, window_minutes=240)
    if upcoming:
        return upcoming[0]
    return None
