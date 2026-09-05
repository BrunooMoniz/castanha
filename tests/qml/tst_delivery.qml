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
      { tag: "saved", value: {status: "ok", remember_id: "note-id"}, expected: "Salvo no Zinom" },
      { tag: "failed_receipt", value: {remember: {ok: false}}, expected: "" },
      { tag: "no_data", value: null, expected: "" }
    ]
  }
  function test_status(data) {
    compare(DeliveryStatus.zinomLine({zinom: data.value}), data.expected)
  }
}
