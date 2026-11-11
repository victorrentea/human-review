"""A sequence's arrows, painted by what they cross (Victor, 11 Nov 2026): a wire, the
database, or nothing at all."""
import seq_arrow_colors as m

TRACE = """@startuml
participant Browser
participant Backend
participant DB
participant NotificationService
participant "«module»\\nCommons" as Commons
participant "SMS gateway"
[-> Browser: [[src://a.ts:1{x} OwnerService.getOwner ↗]]
Browser -> Backend: [[genseq://1{Click for the request as it was sent} GET /api/owners ⊕]]
Backend -> Backend: [[src://B.java:3{x} OwnerRepository.findById ↗]]
Backend -> DB: [[genseq://2{Click for the statement behind this call} select owners ⊕]]
Backend -> Commons: PhoneNumbers.normalize
Backend -> NotificationService: POST /api/notifications
NotificationService -> "SMS gateway": send-sms
NotificationService --> Backend: 202
Backend --> Browser: 200
Backend -[#2E7D32]-> DB: already coloured
@enduml
"""


def arrows(text):
    return [l for l in m.colour(text).splitlines() if " -" in l]


def test_each_arrow_is_painted_by_what_it_crosses():
    got = arrows(TRACE)
    assert got[0].startswith(f"Browser -[{m.NETWORK}]> Backend:")
    assert got[1].startswith("Backend -> Backend:"), "a call inside one process stays plain"
    assert got[2].startswith(f"Backend -[{m.SQL}]> DB:")
    assert got[3].startswith("Backend -> Commons:"), "a «module» is a library, not a system"
    assert got[4].startswith(f"Backend -[{m.NETWORK}]> NotificationService:")
    assert got[5].startswith(f'NotificationService -[{m.NETWORK}]> "SMS gateway":')
    assert got[6].startswith(f"NotificationService -[{m.NETWORK}]-> Backend: 202")
    assert got[7].startswith(f"Backend -[{m.NETWORK}]-> Browser: 200")
    assert got[8].startswith("Backend -[#2E7D32]-> DB:"), "a colour the source chose stays"


def test_a_found_message_from_the_edge_is_left_alone():
    assert "[-> Browser:" in m.colour(TRACE)


def test_a_database_is_known_by_its_traced_statement_even_under_another_name():
    src = ("participant Backend\nparticipant Store\n"
           "Backend -> Store: [[genseq://9{Click for the statement behind this call} select ⊕]]\n"
           "Store --> Backend: rows\n")
    got = m.colour(src).splitlines()
    assert got[2].startswith(f"Backend -[{m.SQL}]> Store")
    assert got[3].startswith(f"Store -[{m.SQL}]-> Backend")


def test_the_colours_are_themed_for_dark_mode():
    from pathlib import Path
    svg = Path(__file__).resolve().parent.parent / "scripts/hrbuild/shared/svg.py"
    text = svg.read_text()
    assert f'"{m.NETWORK}": "--dgm-arrow-net"' in text
    assert f'"{m.SQL}": "--dgm-arrow-sql"' in text
