import QtQuick
import QtTest
import "../../DeliveryStatus.js" as DeliveryStatus

TestCase {
  name: "DeliveryStatus"
  function test_status_data() {
    return [
      { tag: "deleted", value: {status: "tombstoned", remember_id: "old-id"}, expected: "Excluído no Zinom" },
      { tag: "superseded", value: {status: "superseded"}, expected: "Substituído no Zinom" },
      { tag: "facts_pending", value: {status: "pending", remember_id: "note-id", facts_status: "pending_lineage"}, expected: "Nota salva no Zinom; fatos pendentes" },
      { tag: "offline", value: {status: "pending"}, expected: "Envio ao Zinom pendente" },
      { tag: "legacy_token", value: {status: "skipped", reason: "Integração desligada ou sem token"}, expected: "Envio ao Zinom pendente" },
      { tag: "legacy_credentials", value: {status: "skipped", reason: "Credenciais ausentes", remember_id: "old-id"}, expected: "Envio ao Zinom pendente" },
      { tag: "silent", value: {status: "skipped", reason: "Gravação sem áudio, nada para lembrar"}, expected: "Não enviado ao Zinom: Gravação sem áudio, nada para lembrar" },
      { tag: "saved", value: {status: "ok", remember_id: "note-id"}, expected: "Salvo no Zinom" },
      { tag: "corrupt_receipt", value: {status: "error", receipt_source: "legacy-recovery"}, expected: "Envio ao Zinom não confirmado" },
      { tag: "failed_receipt", value: {remember: {ok: false}}, expected: "" },
      { tag: "no_data", value: null, expected: "" }
    ]
  }
  function test_status(data) {
    compare(DeliveryStatus.zinomLine({zinom: data.value}, "pt"), data.expected)
  }

  function test_status_english() {
    compare(DeliveryStatus.zinomLine({zinom: {status: "tombstoned", remember_id: "old-id"}}, "en"), "Deleted in Zinom")
    compare(DeliveryStatus.zinomLine({zinom: {status: "ok", remember_id: "note-id"}}, "en"), "Saved in Zinom")
    compare(DeliveryStatus.zinomLine({zinom: {status: "pending", remember_id: "note-id", facts_status: "pending_lineage"}}, "en"), "Note saved in Zinom; facts pending")
    compare(DeliveryStatus.zinomLine({zinom: {status: "pending"}}, "en"), "Zinom delivery pending")
    compare(DeliveryStatus.zinomLine({zinom: {status: "skipped", reason: "Gravação sem áudio, nada para lembrar"}}, "en"), "Not sent to Zinom: Gravação sem áudio, nada para lembrar")
    compare(DeliveryStatus.zinomLine({zinom: {status: "ok", remember_id: "note-id", facts_ingested: 2}}, "en"), "Saved in Zinom, with 2 facts")
    compare(DeliveryStatus.zinomLine({summary_status: "pending", zinom: {status: "pending"}}, "en"), "Summary pending")
  }

  function test_legacy_projection_is_read_only_and_bound_to_slug() {
    var last = {slug: "fixture", title: "Original", zinom: {status: "error", errors: ["HTTP 530"]}}
    var delivered = {status: "ok", receipt_source: "legacy-recovery"}
    compare(DeliveryStatus.projectedLastResult(last, [{slug: "other", zinom: delivered}]), last)
    var current = DeliveryStatus.projectedLastResult(last, [{slug: "fixture", zinom: delivered}])
    compare(DeliveryStatus.zinomLine(current, "pt"), "Salvo no Zinom")
    compare(current.title, "Original")
    compare(last.zinom.status, "error")
    compare(last.zinom.errors[0], "HTTP 530")
    var corrupt = DeliveryStatus.projectedLastResult(current, [{slug: "fixture", zinom: {
      status: "error", receipt_source: "legacy-recovery"}}])
    compare(DeliveryStatus.zinomLine(corrupt, "pt"), "Envio ao Zinom não confirmado")
    verify(DeliveryStatus.zinomNeedsSync(corrupt))
  }

  function test_pending_data() {
    return [
      { tag: "legacy_token", note: {zinom: {status: "skipped", reason: "Sem token"}}, pending: true },
      { tag: "legacy_disabled", note: {zinom: {status: "skipped", reason: "Integração desligada"}}, pending: true },
      { tag: "legacy_credentials", note: {zinom: {status: "skipped", reason: "Credenciais ausentes"}}, pending: true },
      { tag: "legacy_with_retry", note: {can_retry: true, zinom: {status: "skipped", reason: "Sem token"}}, pending: true },
      { tag: "legacy_with_receipt", note: {zinom: {status: "skipped", reason: "Sem token", remember_id: "old-id"}}, pending: true },
      { tag: "deliberate_skip", note: {zinom: {status: "skipped"}}, pending: false },
      { tag: "silent", note: {zinom: {status: "skipped", reason: "Gravação sem áudio, nada para lembrar"}}, pending: false },
      { tag: "saved", note: {zinom: {status: "ok"}}, pending: false },
      { tag: "deleted", note: {zinom: {status: "tombstoned"}}, pending: false },
      { tag: "superseded", note: {zinom: {status: "superseded"}}, pending: false },
      { tag: "deleted_processing", note: {processing_status: "pending", zinom: {status: "tombstoned"}}, pending: false },
      { tag: "processing", note: {processing_status: "pending", zinom: {status: "ok"}}, pending: true },
      { tag: "new", note: {}, pending: true }
    ]
  }
  function test_pending(data) {
    compare(DeliveryStatus.zinomNeedsSync(data.note), data.pending)
    compare(DeliveryStatus.zinomIcon(data.note), data.pending ? "󰀦  " : "󰄬  ")
  }

  function test_summary_pending_line_and_retry() {
    var note = {processing_status: "pending", summary_status: "pending",
                summary_error: "cota da Groq esgotada (HTTP 429), 4 tentativas", zinom: {status: "pending"}}
    compare(DeliveryStatus.zinomLine(note, "pt"), "Resumo pendente: cota da Groq esgotada (HTTP 429), 4 tentativas")
    verify(DeliveryStatus.zinomNeedsSync(note))
    compare(DeliveryStatus.zinomLine({summary_status: "", zinom: {status: "pending"}}, "pt"), "Envio ao Zinom pendente")
    compare(DeliveryStatus.zinomLine({summary_status: "pending", zinom: {status: "tombstoned"}}, "pt"), "Excluído no Zinom")
  }
}
