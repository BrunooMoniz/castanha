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
}
