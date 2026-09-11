import QtQuick
import QtTest
import "../../StageStatus.js" as StageStatus

TestCase {
    name: "MeetingStages"
    function test_manual_notes_do_not_invent_audio_or_remote_delivery() {
        var n = {source: "manual", summary_status: "not_requested", zinom: {status: "local_only"}}
        compare(StageStatus.stages(n)[0].state, "removed")
        compare(StageStatus.stages(n)[1].state, "removed")
        verify(StageStatus.status(n).label.indexOf("Anotações locais") >= 0)
        n.summary_status = "complete"
        compare(StageStatus.status(n).label, "Resumo local pronto")
        compare(StageStatus.stages(n)[3].state, "removed")
    }
    function test_summary_pending_never_becomes_transcription_failure() {
        var n = {recordings_count: 1, has_transcript: true, summary_status: "pending", summary_error: "Cota esgotada", zinom: {}}
        compare(StageStatus.stages(n)[1].state, "done")
        compare(StageStatus.stages(n)[2].state, "waiting")
        compare(StageStatus.status(n).tone, "waiting")
        verify(StageStatus.status(n).label.indexOf("cota") >= 0)
    }
    function test_confirmation_required() {
        var n = {has_transcript: true, summary_status: "complete", zinom: {status: "pending"}}
        compare(StageStatus.status(n).tone, "waiting")
        n.zinom.status = "ok"
        compare(StageStatus.status(n).tone, "done")
        n.zinom.facts_status = "pending_lineage"
        compare(StageStatus.status(n).tone, "waiting")
        n.zinom.status = "tombstoned"
        compare(StageStatus.status(n).tone, "neutral")
    }
    function test_audio_problem_survives_completed_delivery() {
        var n = {has_transcript: true, summary_status: "complete", zinom: {status: "ok"}, audio_status: "mic_mudo"}
        compare(StageStatus.status(n).tone, "error")
    }
    function test_current_delivery_failure_precedes_old_summary_error() {
        var n = {has_transcript: true, summary_status: "complete", summary_error: "timeout antigo", zinom: {status: "error"}}
        compare(StageStatus.status(n).tone, "error")
        n.summary_status = "pending"
        compare(StageStatus.status(n).tone, "waiting")
    }
    function test_missing_audio_is_not_claimed_preserved() {
        verify(StageStatus.status({}).label.indexOf("preservado") < 0)
    }
}
