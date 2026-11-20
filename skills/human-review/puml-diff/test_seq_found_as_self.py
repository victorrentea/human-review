"""A found message is drawn as the lifeline activating itself (Victor, 20 Nov 2026)."""
import seq_found_as_self as m


def test_a_found_message_becomes_a_self_call():
    src = ("[-> Browser: [[src://fe/a.ts:30{Click} OwnerService.getOwners ↗]]\n"
           "activate Browser\n")
    assert m.rewrite(src) == ("Browser -> Browser: [[src://fe/a.ts:30{Click} OwnerService.getOwners ↗]]\n"
                              "activate Browser\n")


def test_the_delta_keeps_its_colour_and_a_quoted_name_stays_quoted():
    assert m.rewrite("[-[#2E7D32]> Browser: <color:#2E7D32>go</color>") == \
        "Browser -[#2E7D32]> Browser: <color:#2E7D32>go</color>"
    assert m.rewrite('[-> "Pet UI": x') == '"Pet UI" -> "Pet UI": x'


def test_messages_back_to_the_edge_and_ordinary_calls_are_left_alone():
    for line in ("[<- Browser: shown", "Browser ->]: out", "Browser -> Backend: GET /x",
                 "Backend -> Backend: book-visit"):
        assert m.rewrite(line) == line
