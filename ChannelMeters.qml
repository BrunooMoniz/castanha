import QtQuick
import qs.Commons
import qs.Ui
import "AudioMeter.js" as MeterLogic

Column {
    id: root
    property var stateData: ({})
    property bool micMuted: false
    property color foreground: Color.foreground
    property string fontFamily: Style.font.family
    property double nowMs: Date.now()
    readonly property bool recording: stateData.status === "recording"
    readonly property bool paused: stateData.status === "paused"
    readonly property bool dual: stateData.mode !== "mic_only"
    readonly property bool dark: Color.background.hslLightness < 0.5
    // Papéis semânticos, adaptados à luminosidade do tema, sempre distintos.
    readonly property color micColor: Qt.hsla(0.52, 0.68, dark ? 0.69 : 0.32, 1)
    readonly property color callColor: Qt.hsla(0.10, 0.83, dark ? 0.71 : 0.33, 1)
    spacing: Style.space(8)
    Timer { interval: 120; repeat: true; running: root.visible; onTriggered: root.nowMs = Date.now() }
    Repeater {
        model: 2
        delegate: Rectangle {
            id: channel
            required property int index
            readonly property var modelData: index === 0
                ? {label: "Seu microfone", device: root.stateData.mic_device_name || "Microfone selecionado",
                   peak: root.stateData.mic_peak || 0, stamp: root.stateData.mic_peak_updated_at || 0,
                   muted: root.micMuted, enabled: true, tone: root.micColor}
                : {label: "Áudio da chamada", device: root.stateData.call_device_name || "Saída de áudio selecionada",
                   peak: root.stateData.call_peak || 0, stamp: root.stateData.call_peak_updated_at || 0,
                   muted: false, enabled: root.dual, tone: root.callColor}
            readonly property bool fresh: MeterLogic.isFresh(modelData.stamp, Math.max(root.nowMs, Date.now()), root.recording)
            readonly property bool live: fresh && modelData.enabled && !modelData.muted
            width: root.width
            height: Style.space(82)
            radius: Style.cornerRadius
            color: Qt.rgba(modelData.tone.r, modelData.tone.g, modelData.tone.b, 0.07)
            border.width: 1
            border.color: Qt.rgba(modelData.tone.r, modelData.tone.g, modelData.tone.b, 0.25)
            opacity: modelData.enabled ? 1 : 0.55
            Text {
                anchors { left: parent.left; top: parent.top; margins: Style.space(9) }
                text: channel.modelData.label
                color: channel.modelData.tone
                font.family: root.fontFamily
                font.pixelSize: Style.font.caption
                font.bold: true
            }
            Text {
                anchors { right: parent.right; top: parent.top; margins: Style.space(9) }
                text: !channel.modelData.enabled ? "Desativado" : channel.modelData.muted ? "Mudo"
                    : root.paused ? "Pausado" : !root.recording ? "Pronto"
                    : !channel.fresh ? "Sem medição" : channel.modelData.peak > 0.01 ? "Recebendo áudio" : "Silêncio"
                color: root.foreground
                font.family: root.fontFamily
                font.pixelSize: Style.font.caption
            }
            ChannelWaveform {
                anchors { left: parent.left; right: parent.right; bottom: parent.bottom; margins: Style.space(9); bottomMargin: Style.space(26) }
                height: Style.space(25)
                peak: channel.live ? channel.modelData.peak : 0
                active: channel.live
                ink: channel.modelData.tone
            }
            Text {
                anchors { left: parent.left; right: parent.right; bottom: parent.bottom; margins: Style.space(9); bottomMargin: Style.space(6) }
                text: channel.modelData.device
                color: root.foreground
                opacity: 0.72
                font.family: root.fontFamily
                font.pixelSize: Style.font.caption
                elide: Text.ElideMiddle
            }
            // O dispositivo é consultável sem duplicar informação em cada quadro.
            MouseArea { id: hover; anchors.fill: parent; acceptedButtons: Qt.NoButton; hoverEnabled: true }
            PanelToolTip { visible: hover.containsMouse; text: channel.modelData.device; fontFamily: root.fontFamily }
        }
    }
}
