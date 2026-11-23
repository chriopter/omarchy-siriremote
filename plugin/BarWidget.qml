import QtQuick
import Quickshell
import Quickshell.Io
import Quickshell.Wayland
import qs.Ui
import qs.Commons
import qs.Commons as Commons

// The Siri Remote in the Omarchy bar. The driver (src/remote.py, a systemd user
// service set up by bin/setup) writes its live state to
// $XDG_RUNTIME_DIR/omarchy-siriremote/state.json, with a timestamp it renews
// every five seconds, and reads the touch settings from
// ~/.local/share/omarchy-siriremote/settings.json, which this card writes.
// No file or an old timestamp means no driver: the card then offers to set it
// up or start it.
BarWidget {
  id: root
  moduleName: "chriopter.siriremote"

  readonly property string runDir: (Quickshell.env("XDG_RUNTIME_DIR") || "/tmp") + "/omarchy-siriremote"
  readonly property string dataDir: Quickshell.env("SIRI_REMOTE_DATA")
    || (Quickshell.env("XDG_DATA_HOME") || Quickshell.env("HOME") + "/.local/share") + "/omarchy-siriremote"
  // The repository's bin/setup, beside the plugin folder (also through a symlinked plugin folder).
  readonly property string setupPath: Qt.resolvedUrl("../bin/setup").toString().replace(/^file:\/\//, "")

  property var live: ({})
  property real now: Date.now()
  readonly property bool running: typeof live.t === "number" && now / 1000 - live.t < 15
  property string unit: ""          // loaded | not-found | "" not asked yet
  property var touch: ({})
  property bool popupOpen: false

  // Defaults of VMouse in src/remote.py.
  readonly property var touchDefaults: ({ gain: 1.9, k: 0.005, precision: 0.45, roll: 6, smooth: 0.005, land_s: 0.045 })
  readonly property bool connected: running && !!live.connected
  readonly property bool listening: connected && !!live.listening
  readonly property bool low: connected && live.percent !== null && live.percent !== undefined
    && live.percent < 20 && !live.charging

  readonly property string statusText: !running
    ? strings.t(unit === "not-found" ? "status.missing" : "status.stopped")
    : listening ? strings.t("status.listening")
    : connected && live.full ? strings.t("status.connected") + " · " + strings.t("battery.full")
    : connected && live.charging ? strings.t("status.connected") + " · " + strings.t("battery.charging")
    : connected ? strings.t("status.connected")
    : live.searching ? strings.t("status.asleep")
    : strings.t("status.idle")
  readonly property bool hasBattery: connected && live.percent !== null && live.percent !== undefined
  readonly property string batteryText: hasBattery
    ? strings.t("battery.percent").arg(Commons.I18n.formatNumber(live.percent, 0)) : ""
  // Nerd Font battery: charging bolt, or a level in tenths.
  readonly property string batteryGlyph: {
    if (!hasBattery) return ""
    if (live.charging && !live.full) return "󰂄"
    var levels = ["󰂎", "󰁺", "󰁻", "󰁼", "󰁽", "󰁾", "󰁿", "󰂀", "󰂁", "󰂂", "󰁹"]
    return levels[Math.max(0, Math.min(10, Math.round(live.percent / 10)))]
  }

  function close() { popupOpen = false }   // PopupCard calls this on its owner
  onPopupOpenChanged: if (popupOpen) unitQuery.running = true

  function touchValue(key) {
    var v = touch[key]
    return typeof v === "number" ? v : touchDefaults[key]
  }
  function setTouch(key, value) {
    var next = Object.assign({}, touch)
    if (value === undefined) delete next[key]
    else next[key] = Math.round(value * 1000) / 1000
    touch = next
    saveTimer.restart()
  }

  Commons.Translations { id: strings; directory: Qt.resolvedUrl("i18n") }

  implicitWidth: icon.implicitWidth + Style.space(14)
  implicitHeight: barSize

  FileView {
    id: stateFile
    path: root.runDir + "/state.json"
    watchChanges: true
    printErrors: false
    onFileChanged: reload()
    onLoaded: { try { root.live = JSON.parse(text()) || {} } catch (e) { root.live = {} } }
    onLoadFailed: root.live = {}
  }

  // A file that does not exist yet is not watched, so look for it while there
  // is no driver; and a driver that crashed leaves its file behind, so check
  // the timestamp's age.
  Timer {
    interval: 5000
    repeat: true
    running: true
    onTriggered: {
      root.now = Date.now()
      if (!root.running) stateFile.reload()
    }
  }
  onRunningChanged: if (!running) unitQuery.running = true

  Process {
    id: unitQuery
    command: ["systemctl", "--user", "show", "-p", "LoadState", "--value", "siri-remote.service"]
    stdout: StdioCollector {
      waitForEnd: true
      onStreamFinished: root.unit = text.trim()
    }
  }

  FileView {
    id: settingsFile
    path: root.dataDir + "/settings.json"
    watchChanges: true
    atomicWrites: true
    printErrors: false
    onFileChanged: reload()
    onLoaded: { try { root.touch = JSON.parse(text()) || {} } catch (e) { root.touch = {} } }
  }

  // Slider moves arrive many times a second; write once they settle. The driver
  // reads the file again when the next touch begins.
  Timer {
    id: saveTimer
    interval: 250
    onTriggered: settingsFile.setText(JSON.stringify(root.touch, null, 1) + "\n")
  }

  readonly property color iconColor: root.low ? Color.urgent
    : root.connected ? root.bar.barForeground
    : Qt.darker(root.bar.barForeground, root.running ? 1.6 : 2.4)

  // The remote itself, drawn: a slim rounded body with the touch ring at the top
  // and two buttons below. The ring fills while Siri listens.
  Item {
    id: icon
    readonly property real size: Style.font.icon
    readonly property real stroke: Math.max(1.5, Math.round(size / 11))
    anchors.centerIn: parent
    implicitWidth: Math.round(size * 0.5)
    implicitHeight: Math.round(size * 0.95)

    Rectangle {
      anchors.fill: parent
      radius: width * 0.38
      color: "transparent"
      border.width: icon.stroke
      border.color: root.iconColor
    }
    Rectangle {
      id: ring
      width: Math.round(icon.width * 0.56)
      height: width
      radius: width / 2
      anchors.horizontalCenter: parent.horizontalCenter
      y: Math.round(icon.width * 0.24)
      color: root.listening ? root.iconColor : "transparent"
      border.width: icon.stroke
      border.color: root.iconColor
    }
    Repeater {
      model: 2
      Rectangle {
        width: icon.stroke * 1.6
        height: width
        radius: width / 2
        anchors.horizontalCenter: parent.horizontalCenter
        y: ring.y + ring.height + icon.stroke * 1.8 + index * icon.stroke * 2.6
        color: root.iconColor
      }
    }
  }

  MouseArea {
    anchors.fill: parent
    cursorShape: Qt.PointingHandCursor
    onClicked: root.popupOpen = !root.popupOpen
  }

  component Label: Text {
    textFormat: Text.PlainText
    color: Qt.darker(root.bar.foreground, 1.3)
    font.family: root.bar.fontFamily
    font.pixelSize: Style.font.bodySmall
    wrapMode: Text.WordWrap
  }

  component Action: Button {
    width: parent.width
    bordered: true
    foreground: root.bar.foreground
  }

  // One row of the card: its name on the left, the control on the right.
  component Field: Item {
    id: field
    property string label: ""
    default property alias control: holder.children
    readonly property real controlWidth: holder.width
    width: parent.width
    height: Math.max(name.implicitHeight, holder.childrenRect.height)
    Label {
      id: name
      anchors.verticalCenter: parent.verticalCenter
      text: field.label
    }
    Item {
      id: holder
      anchors.right: parent.right
      width: parent.width - Style.space(150)
      height: childrenRect.height
    }
  }

  component TouchSlider: Field {
    id: row
    property string key: ""
    property string name: ""
    property real minimum: 0
    property real maximum: 1
    property real step: 0.01
    property real scale: 1
    property int decimals: 0
    property string unit: ""
    label: name + "  " + Commons.I18n.formatNumber(root.touchValue(key) * scale, decimals) + unit
    PanelSlider {
      bar: root.bar
      width: row.controlWidth
      minimum: row.minimum
      maximum: row.maximum
      step: row.step
      value: root.touchValue(row.key)
      onMoved: function(v) { root.setTouch(row.key, v) }
    }
  }

  // A section that opens and closes under its own header.
  component Section: Column {
    id: section
    property string title: ""
    property bool expanded: false
    default property alias content: body.children
    width: parent.width
    spacing: Style.space(8)
    Item {
      width: parent.width
      height: header.implicitHeight + Style.space(4)
      Text {
        id: header
        anchors.verticalCenter: parent.verticalCenter
        textFormat: Text.PlainText
        text: (section.expanded ? "▾  " : "▸  ") + section.title
        color: root.bar.foreground
        font.family: root.bar.fontFamily
        font.pixelSize: Style.font.body
      }
      MouseArea {
        anchors.fill: parent
        cursorShape: Qt.PointingHandCursor
        onClicked: section.expanded = !section.expanded
      }
    }
    Column {
      id: body
      width: parent.width
      visible: section.expanded
      spacing: Style.space(8)
    }
  }

  // The settings menu the remote opens (Siri double press). The driver owns it: it publishes the
  // rows and the selection in state.json, this only draws them.
  readonly property var menu: running && live.menu && live.menu.open ? live.menu : null
  // Drawn like Omarchy's own menu: a large card in the middle of the screen over a scrim. It
  // takes no keyboard focus; the remote's ring drives it through the driver.
  OverlayWindow {
    id: menuWindow
    shown: root.menu !== null
    shownKeyboardFocus: WlrKeyboardFocus.None
    WlrLayershell.namespace: "omarchy-siriremote-menu"

    readonly property int rowHeight: Math.max(Style.space(64), Style.font.heading + Style.space(36))

    Rectangle {
      anchors.fill: parent
      color: Commons.Color.menu.scrim
    }

    BorderSurface {
      id: menuCard
      width: Math.min(Style.space(640), menuWindow.width - Style.gapsOut * 2)
      height: menuColumn.implicitHeight + Style.spacing.panelPadding * 2
      anchors.centerIn: parent
      radius: Style.cornerRadius
      color: Commons.Color.menu.background
      borderSpec: Border.surfaceSpec("menu", "border", Commons.Color.menu.border, Math.max(1, Style.space(2)))
      padding: Style.spacing.panelPadding

      Column {
        id: menuColumn
        anchors.left: parent.left
        anchors.right: parent.right
        anchors.top: parent.top
        anchors.margins: Style.spacing.panelPadding
        spacing: Style.spacing.xs

        Text {
          width: parent.width
          height: Math.max(Style.space(44), Style.font.heading + Style.space(16))
          verticalAlignment: Text.AlignVCenter
          textFormat: Text.PlainText
          text: strings.t("menu.title")
          color: Qt.darker(Commons.Color.menu.text, 1.25)
          font.family: Style.font.menuFamily
          font.pixelSize: Style.font.heading
        }
        Repeater {
          model: root.menu ? root.menu.rows : []
          Rectangle {
            required property var modelData
            required property int index
            readonly property bool chosen: !!root.menu && root.menu.sel === index
            width: menuColumn.width
            height: menuWindow.rowHeight
            radius: Style.cornerRadius
            color: chosen ? Commons.Color.menu.selectedBackground : "transparent"
            Text {
              anchors.left: parent.left
              anchors.leftMargin: Style.space(18)
              anchors.verticalCenter: parent.verticalCenter
              textFormat: Text.PlainText
              text: strings.t("menu." + modelData.key)
              color: chosen ? Commons.Color.menu.selectedText : Commons.Color.menu.text
              font.family: Style.font.menuFamily
              font.pixelSize: Style.font.heading
            }
            Text {
              anchors.right: parent.right
              anchors.rightMargin: Style.space(18)
              anchors.verticalCenter: parent.verticalCenter
              width: parent.width * 0.55
              horizontalAlignment: Text.AlignRight
              elide: Text.ElideMiddle
              textFormat: Text.PlainText
              text: chosen ? "‹  " + modelData.value + "  ›" : modelData.value
              color: chosen ? Commons.Color.menu.selectedText : Qt.darker(Commons.Color.menu.text, 1.25)
              font.family: Style.font.menuFamily
              font.pixelSize: Style.font.heading
            }
          }
        }
        Text {
          width: parent.width
          height: Math.max(Style.space(40), Style.font.bodySmall + Style.space(20))
          verticalAlignment: Text.AlignBottom
          horizontalAlignment: Text.AlignHCenter
          textFormat: Text.PlainText
          text: strings.t("menu.hint")
          color: Qt.darker(Commons.Color.menu.text, 1.5)
          font.family: Style.font.menuFamily
          font.pixelSize: Style.font.body
        }
      }
    }
  }

  PopupCard {
    id: popup
    anchorItem: root
    bar: root.bar
    owner: root
    open: root.popupOpen
    contentWidth: popup.fittedContentWidth(Style.space(330))
    contentHeight: popup.fittedContentHeight(column.implicitHeight)

    Column {
      id: column
      width: parent.width
      spacing: Style.space(8)

      // Name on the left, the battery large on the right.
      Item {
        width: parent.width
        height: Math.max(title.implicitHeight, battery.implicitHeight)
        Text {
          id: title
          anchors.verticalCenter: parent.verticalCenter
          textFormat: Text.PlainText
          text: strings.t("title")
          color: root.bar.foreground
          font.family: root.bar.fontFamily
          font.pixelSize: Style.font.subtitle
          font.bold: true
        }
        Text {
          id: battery
          anchors.right: parent.right
          anchors.verticalCenter: parent.verticalCenter
          visible: root.batteryText !== ""
          textFormat: Text.PlainText
          text: root.batteryGlyph + "  " + root.batteryText
          color: root.low ? Color.urgent : root.bar.foreground
          font.family: root.bar.fontFamily
          font.pixelSize: Style.font.subtitle
        }
      }
      Label {
        width: parent.width
        text: root.statusText
          + (root.connected && typeof root.live.radio_ms === "number" ? " · " + strings.t("status.radio").arg(root.live.radio_ms) : "")
      }

      Field {
        visible: !root.running && root.unit !== ""
        label: strings.t("field.driver")
        Action {
          width: parent.width
          text: strings.t(root.unit === "not-found" ? "action.setup" : "action.start")
          onClicked: {
            if (root.unit === "not-found") {
              root.bar.run("omarchy-launch-floating-terminal-with-presentation " + Util.shellQuote(root.setupPath))
              root.popupOpen = false
            } else {
              root.bar.run("systemctl --user restart siri-remote.service")
            }
          }
        }
      }

      // The driver stops searching after two minutes; this starts it again.
      Field {
        visible: root.running && !root.connected && !root.live.searching
        label: strings.t("field.remote")
        Action {
          width: parent.width
          text: strings.t("action.search")
          onClicked: root.bar.run("systemctl --user kill -s USR1 siri-remote.service")
        }
      }

      PanelSeparator { visible: root.running; width: parent.width; foreground: root.bar.foreground }
      Section {
        visible: root.running
        title: strings.t("section.settings")
        TouchSlider { key: "gain"; name: strings.t("touch.speed"); minimum: 0.5; maximum: 4; decimals: 1; unit: "×" }
        TouchSlider { key: "k"; name: strings.t("touch.acceleration"); minimum: 0; maximum: 0.03; step: 0.001; scale: 1000 }
        TouchSlider { key: "precision"; name: strings.t("touch.precision"); minimum: 0; maximum: 0.9; step: 0.05; scale: 100; unit: " %" }
        TouchSlider { key: "roll"; name: strings.t("touch.roll"); minimum: 0; maximum: 12; step: 0.5; decimals: 1 }
        TouchSlider { key: "smooth"; name: strings.t("touch.smoothing"); minimum: 0; maximum: 0.06; step: 0.005; scale: 1000; unit: " ms" }
        TouchSlider { key: "land_s"; name: strings.t("touch.landing"); minimum: 0; maximum: 0.4; step: 0.01; scale: 1000; unit: " ms" }
      }
      // Always at hand: start the driver over, see its log, take the touch settings back to
      // the defaults.
      Row {
        visible: root.running
        width: parent.width
        spacing: Style.space(6)
        Action {
          width: (parent.width - parent.spacing * 2) / 3
          text: strings.t("action.restart")
          onClicked: root.bar.run("systemctl --user restart siri-remote.service")
        }
        Action {
          width: (parent.width - parent.spacing * 2) / 3
          text: strings.t("action.logs")
          onClicked: {
            root.bar.run("omarchy-launch-floating-terminal-with-presentation journalctl --user -u siri-remote.service -f")
            root.popupOpen = false
          }
        }
        Action {
          width: (parent.width - parent.spacing * 2) / 3
          text: strings.t("touch.reset")
          onClicked: ["gain", "k", "precision", "roll", "smooth", "land_s", "glide"].forEach(function(key) { root.setTouch(key) })
        }
      }
    }
  }
}
