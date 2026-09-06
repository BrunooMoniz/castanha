import QtQuick
import QtTest
import "../../DeliveryStatus.js" as DeliveryStatus

TestCase {
  name: "DeliveryStatus"
  function test_status_data() {
    return [
      { tag: "deleted", value: {status: "tombstoned", remember_id: "old-id"}, expected: "Excluído no Zinom" },
      { tag: "facts_pending", value: {status: "pending", remember_id: "note-id", facts_status: "pending_lineage"}, expected: "Nota salva no Zinom; fatos pendentes" },
      { tag: "offline", value: {status: "pending"}, expected: "Envio ao Zinom pendente" },
      { tag: "legacy_token", value: {status: "skipped", reason: "Integração desligada ou sem token"}, expected: "Envio ao Zinom pendente" },
      { tag: "legacy_credentials", value: {status: "skipped", reason: "Credenciais ausentes", remember_id: "old-id"}, expected: "Envio ao Zinom pendente" },
      { tag: "silent", value: {status: "skipped", reason: "Gravação sem áudio, nada para lembrar"}, expected: "Não enviado ao Zinom: Gravação sem áudio, nada para lembrar" },
      { tag: "saved", value: {status: "ok", remember_id: "note-id"}, expected: "Salvo no Zinom" },
      { tag: "failed_receipt", value: {remember: {ok: false}}, expected: "" },
      { tag: "no_data", value: null, expected: "" }
    ]
  }
  function test_status(data) {
    compare(DeliveryStatus.zinomLine({zinom: data.value}), data.expected)
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
      { tag: "deleted_processing", note: {processing_status: "pending", zinom: {status: "tombstoned"}}, pending: false },
      { tag: "processing", note: {processing_status: "pending", zinom: {status: "ok"}}, pending: true },
      { tag: "new", note: {}, pending: true }
    ]
  }
  function test_pending(data) {
    compare(DeliveryStatus.zinomNeedsSync(data.note), data.pending)
    compare(DeliveryStatus.zinomIcon(data.note), data.pending ? "󰀦  " : "󰄬  ")
  }
}
