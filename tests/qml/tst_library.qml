import QtQuick
import QtTest
import "../../LibraryLogic.js" as Logic

TestCase {
  name: "LibraryLogic"
  function test_search_and_status_cover_full_inventory_without_mutation() {
    var items = [{slug: "one", title: "Nora produto", when: "2026-09-10", status: "pending"},
                 {slug: "two", title: "Primata", when: "2026-09-11", status: "complete"}]
    compare(Logic.filtered(items, "NORA", "all").length, 1)
    compare(Logic.filtered(items, "", "pending")[0].slug, "one")
    compare(Logic.filtered(items, "", "complete")[0].slug, "two")
    compare(Logic.filtered(items, "2026-09-11", "all")[0].slug, "two")
    compare(Logic.filtered(items, "missing", "all").length, 0)
    compare(items.length, 2)
  }
  function test_iso_date_and_event_label_never_invent_values() {
    compare(Logic.isoDate("2026-09-12T11:29:50.281638"), "2026-09-12")
    compare(Logic.isoDate(""), "")
    compare(Logic.isoDate("hoje"), "")
    compare(Logic.eventLabel({start: "2026-09-12T10:30:00-03:00", title: "Nora Weekly", account: "moniz@nora.finance", attendees: [{}, {}]}), "10:30 · Nora Weekly · moniz · 2 convidados")
    compare(Logic.eventLabel({start: "2026-09-12T10:30:00-03:00", title: "Solo", attendees: [{}]}), "10:30 · Solo · 1 convidado")
    compare(Logic.eventLabel({title: ""}), "--:-- · Reunião")
  }
  function test_move_targets_put_same_day_first_and_exclude_current() {
    var items = [{slug: "hoje-b", title: "B", when: "2026-09-12T15:00:00"},
                 {slug: "ontem", title: "Ontem", when: "2026-09-11T10:00:00"},
                 {slug: "atual", title: "Atual", when: "2026-09-12T11:00:00"},
                 {slug: "hoje-a", title: "A", when: "2026-09-12T09:00:00"},
                 {slug: "sem-data", title: ""}]
    var targets = Logic.moveTargets(items, "atual", "2026-09-12T11:00:00", 20)
    compare(targets.map(function(t) { return t.slug }), ["hoje-b", "hoje-a", "ontem", "sem-data"])
    compare(targets[3].title, "sem-data")
    compare(Logic.moveTargets(items, "atual", "", 2).length, 2)
    compare(items.length, 5)
  }
  function test_time_format_never_invents_missing_timestamps() {
    compare(Logic.clock(0), "0:00")
    compare(Logic.clock(65), "1:05")
    compare(Logic.clock(3670), "1:01:10")
    compare(Logic.date(""), "Data não informada")
  }
  function test_audio_allows_only_local_file_urls() {
    verify(Logic.localAudio("file:///tmp/fixture.wav"))
    verify(!Logic.localAudio("https://example.invalid/recording.ogg"))
    verify(!Logic.localAudio("file://remote-server/recording.ogg"))
    verify(!Logic.localAudio("/tmp/fixture.wav"))
  }
  function test_plain_text_keeps_html_as_text_and_does_not_follow_links() {
    compare(Logic.plainNotes("## Resumo\n**Texto** <img src='https://invalid'>"), "Resumo\nTexto <img src='https://invalid'>")
  }
  function test_summary_blocks_keep_hierarchy_and_only_omit_matching_first_title() {
    var source = "# Produto\n\n## Resumo executivo\nTexto integral <img src='https://invalid'>\n\n### Detalhes\nOutra linha."
    var doc = Logic.notesDocument(source, "Produto")
    compare(doc.blocks.length, 4)
    compare(doc.blocks[0].level, 2)
    compare(doc.blocks[0].text, "Resumo executivo")
    compare(doc.blocks[1].text, "Texto integral <img src='https://invalid'>")
    compare(doc.blocks[2].level, 3)
    compare(Logic.notesDocument(source, "Outro título").blocks[0].text, "Produto")
    compare(Logic.notesDocument("Texto antes\n# Produto", "Produto").blocks[1].text, "Produto")
  }
  function test_extracted_facts_only_remove_identical_full_lines_in_matching_section() {
    var notes = "## Decisões\n• Publicar o protótipo.\n\n## Próximos passos\n- Preparar demonstração\n\n## Contexto\nMudar o contrato."
    var doc = Logic.notesDocument(notes, "")
    var decisions = Logic.extraFacts(["Publicar o protótipo.", "Publicar o protótipo amanhã.", "Mudar o contrato."], doc, "decisions")
    compare(decisions.length, 2)
    compare(decisions[0], "Publicar o protótipo amanhã.")
    compare(decisions[1], "Mudar o contrato.")
    compare(Logic.extraFacts(["Preparar demonstração", "Preparar demonstração · Responsável: Ana"], doc, "actions").length, 1)
    compare(Logic.extraFacts(["Decisão adicional"], Logic.notesDocument("## Decisões", ""), "decisions").length, 1)
    compare(notes, "## Decisões\n• Publicar o protótipo.\n\n## Próximos passos\n- Preparar demonstração\n\n## Contexto\nMudar o contrato.")
  }
  function test_heading_colon_and_single_line_fence_preserve_structure() {
    var doc = Logic.notesDocument("```codigo```\n## Decisões:\n- Publicar o protótipo.", "")
    compare(doc.blocks[0].text, "```codigo```")
    compare(doc.blocks[1].level, 2)
    compare(Logic.extraFacts(["Publicar o protótipo."], doc, "decisions").length, 0)
  }

}
