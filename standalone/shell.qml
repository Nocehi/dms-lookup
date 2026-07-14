import QtQuick
import Quickshell
import "qml" as DankLookup

ShellRoot {
    DankLookup.LookupApplication {
        ipcTarget: "dank-lookup"
    }
}
