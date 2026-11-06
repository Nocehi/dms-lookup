import QtQuick
import Quickshell
import Quickshell.Io

Item {
    id: root

    property string ipcTarget: "dank-lookup"
    property string backendCommand: Quickshell.env("DANK_LOOKUP_COMMAND") || "dank-lookup"
    property string selectionCommand: Quickshell.env("DANK_LOOKUP_SELECTION_COMMAND")
                                      || "dank-lookup-selection"
    property string configPath: Quickshell.env("DANK_LOOKUP_CONFIG") || ""
    // Optional headword font override; empty selects Jigmo, else Asebi Mincho.
    property string headwordFontFamily: Quickshell.env("DANK_LOOKUP_HEADWORD_FONT") || ""
    property bool windowVisible: false
    property var testThemeSnapshot: ({})

    readonly property bool benchmarkEnabled:
        Quickshell.env("DANK_LOOKUP_BENCHMARK") === "1"
        || Quickshell.env("DANK_LOOKUP_BENCHMARK") === "true"
    readonly property bool mockIpcEnabled:
        Quickshell.env("DANK_LOOKUP_ENABLE_MOCK_IPC") === "1"

    // Minimal request state: idle -> loading -> ready|error. Every completion
    // is matched against both sequence and request id before it can mutate UI.
    property string phase: "idle"
    property int sequence: 0
    property string requestId: ""
    property string queryText: ""
    property string selectionSource: "selection"
    property string errorText: ""
    property var activeProcess: null
    property string requestedMode: ""
    property string requestedTokenKey: ""
    property string captureStatus: ""
    property string primaryCaptureStatus: ""
    property string clipboardCaptureStatus: ""

    property var response: ({})
    property var topResultsByMode: ({})
    property var topRelatedByMode: ({})
    property var resultsByMode: ({})
    property var relatedByMode: ({})
    property var availableModes: []
    property var unavailableModes: []
    property var incompleteModes: []
    property var incompleteTokens: []
    property var candidateModes: []
    property string activeMode: ""
    property bool showRelated: false
    property bool segmented: false
    property var tokens: []
    property var tokenItems: []
    property var resultsByToken: ({})
    property string activeToken: ""
    property string activeTokenKey: ""

    // Opt-in, in-memory-only benchmark seam. Durations are monotonic nanoseconds
    // from request acceptance; neither query text nor result content is exposed.
    property double lookupStartedNs: -1
    property double firstQmlFrameTickNs: -1
    property double resultQmlFrameTickNs: -1
    property bool pendingFirstQmlFrameTick: false
    property bool pendingResultQmlFrameTick: false
    property var backendTiming: ({})

    signal showRequested
    signal closeRequested
    signal scrollResetRequested
    signal testCopyRequested
    signal testOpenRequested

    visible: false
    width: 0
    height: 0

    ElapsedTimer {
        id: requestTimer
    }

    function trimmed(value) {
        return String(value === undefined || value === null ? "" : value).trim()
    }

    function makeRequestId(nextSequence) {
        // The UUID is correlation state only and is never persisted or logged.
        // Sequence remains the ordering authority; UUID prevents accidental
        // cross-process/request reuse after a frontend restart.
        var seed = Date.now() + nextSequence
        return "xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx".replace(/[xy]/g,
            function(character) {
                var random = (seed + Math.random() * 16) % 16 | 0
                seed = Math.floor(seed / 16)
                return (character === "x" ? random : (random & 0x3) | 0x8)
                    .toString(16)
            })
    }

    function clearResultState() {
        response = ({})
        topResultsByMode = ({})
        topRelatedByMode = ({})
        resultsByMode = ({})
        relatedByMode = ({})
        availableModes = []
        unavailableModes = []
        incompleteModes = []
        incompleteTokens = []
        candidateModes = []
        activeMode = ""
        showRelated = false
        segmented = false
        tokens = []
        tokenItems = []
        resultsByToken = ({})
        activeToken = ""
        activeTokenKey = ""
        backendTiming = ({})
    }

    function prepareRequest(source) {
        cancelActiveRequest()
        sequence += 1
        requestId = makeRequestId(sequence)
        queryText = ""
        selectionSource = trimmed(source) || "selection"
        captureStatus = ""
        primaryCaptureStatus = ""
        clipboardCaptureStatus = ""
        requestedMode = ""
        requestedTokenKey = ""
        errorText = ""
        phase = "loading"
        clearResultState()

        if (benchmarkEnabled) {
            requestTimer.restartNs()
            lookupStartedNs = -1
            firstQmlFrameTickNs = -1
            resultQmlFrameTickNs = -1
            pendingFirstQmlFrameTick = true
            pendingResultQmlFrameTick = false
        }

        showRequested()
        scrollResetRequested()

        return requestId
    }

    function cancelActiveRequest() {
        if (!activeProcess || !activeProcess.running)
            return
        if (activeProcess.transportRequest === true) {
            var cancelPayload = JSON.stringify({
                action: "cancel",
                request_id: requestId,
                sequence: sequence
            })
            var cancel = cancelProcess.createObject(root, {
                payload: cancelPayload,
                command: [backendCommand, "backend-request"]
            })
            if (cancel)
                cancel.running = true
        } else {
            activeProcess.signal(15)
        }
        activeProcess = null
    }

    function startPreparedCapture(correlationId, source) {
        if (correlationId !== requestId || phase !== "loading")
            return "stale"
        var process = selectionCaptureProcess.createObject(root, {
            requestSequence: sequence,
            correlationId: requestId,
            captureSource: trimmed(source) || selectionSource || "selection",
            command: [selectionCommand, "--capture"]
        })
        if (!process) {
            phase = "error"
            errorText = "Unable to capture selected text"
            return requestId
        }
        activeProcess = process
        process.running = true
        return requestId
    }

    function finishCapture(processSequence, correlationId, captureSource,
                           exitCode, stdoutText) {
        if (processSequence !== sequence || correlationId !== requestId)
            return
        activeProcess = null
        if (exitCode !== 0) {
            phase = "error"
            errorText = "Unable to capture selected text"
            return
        }
        var fields = String(stdoutText || "").split("\n")
        if (fields.length < 6 || fields[0] !== "capture-v1") {
            phase = "error"
            errorText = "Selection capture returned invalid data"
            return
        }
        captureStatus = trimmed(fields[1])
        var selectedSource = trimmed(fields[2]) || captureSource
        primaryCaptureStatus = trimmed(fields[3])
        clipboardCaptureStatus = trimmed(fields[4])
        var capturedQuery = fields.slice(5).join("\n")
        if (captureStatus !== "success_nonempty" || trimmed(capturedQuery) === "") {
            selectionSource = selectedSource
            phase = "error"
            if (captureStatus === "timeout")
                errorText = "Selection capture timed out"
            else if (captureStatus === "unavailable")
                errorText = "Selection capture unavailable"
            else if (captureStatus === "command_error")
                errorText = "Selection capture failed"
            else
                errorText = "No selected text"
            if (benchmarkEnabled)
                pendingResultQmlFrameTick = true
            return
        }
        startPreparedLookup(correlationId, capturedQuery,
                            selectedSource || captureSource,
                            "initial_selection", "")
    }

    function startPreparedLookup(correlationId, query, source, action, mode) {
        if (correlationId !== requestId || phase !== "loading")
            return "stale"

        if (benchmarkEnabled)
            lookupStartedNs = requestTimer.elapsedNs()

        var normalized = trimmed(query)
        queryText = normalized
        selectionSource = trimmed(source) || selectionSource || "selection"
        requestedMode = trimmed(mode)
        scrollResetRequested()

        if (normalized === "") {
            phase = "error"
            errorText = "No selected text"
            if (benchmarkEnabled)
                pendingResultQmlFrameTick = true
            return requestId
        }

        var envelope = {
            action: trimmed(action) || "lookup",
            request_id: requestId,
            sequence: sequence,
            source: selectionSource,
            mode: requestedMode,
            query: normalized
        }
        if (configPath !== "")
            envelope.config = configPath

        var process = lookupProcess.createObject(root, {
            requestSequence: sequence,
            correlationId: requestId,
            payload: JSON.stringify(envelope),
            command: [backendCommand, "backend-request"]
        })
        if (!process) {
            phase = "error"
            errorText = "Unable to start lookup backend"
            if (benchmarkEnabled)
                pendingResultQmlFrameTick = true
            return requestId
        }
        activeProcess = process
        process.running = true
        return requestId
    }

    function startFollowupLookup(action, source, mode) {
        var currentQuery = queryText
        var currentTokenKey = activeTokenKey
        if (trimmed(currentQuery) === "")
            return ""
        var nextSource = trimmed(source) || selectionSource || "selection"
        var id = prepareRequest(nextSource)
        requestedTokenKey = currentTokenKey
        return startPreparedLookup(id, currentQuery, nextSource, action, mode)
    }

    function changeSource(source) {
        return startFollowupLookup("source_change", source, activeMode) !== ""
    }

    function relookup(source, mode) {
        return startFollowupLookup("relookup", source, mode) !== ""
    }

    function finishLookup(processSequence, correlationId, exitCode,
                          stdoutText, stderrText) {
        if (processSequence !== sequence || correlationId !== requestId)
            return

        activeProcess = null
        var data
        try {
            data = JSON.parse(String(stdoutText || ""))
        } catch (error) {
            phase = "error"
            errorText = exitCode === 0
                    ? "Lookup returned invalid data"
                    : "Lookup backend failed (exit " + exitCode + ")"
            if (benchmarkEnabled)
                pendingResultQmlFrameTick = true
            return
        }

        if ((data.sequence !== undefined
             && Number(data.sequence) !== processSequence)
                || (data.request_id !== undefined
                    && String(data.request_id) !== correlationId))
            return

        applyResponse(data)
    }

    function modeKeys(map) {
        return map && typeof map === "object" ? Object.keys(map) : []
    }

    function chooseMode(preferred, modes, map) {
        if (preferred && modes.indexOf(preferred) >= 0 && map[preferred])
            return preferred
        for (var i = 0; i < modes.length; ++i) {
            if (map[modes[i]])
                return modes[i]
        }
        return ""
    }

    function applyResponse(data) {
        response = data || ({})
        topResultsByMode = data.results_by_mode || ({})
        topRelatedByMode = data.related_by_mode || ({})
        resultsByMode = topResultsByMode
        relatedByMode = topRelatedByMode
        availableModes = data.available_modes || modeKeys(resultsByMode)
        unavailableModes = data.unavailable_modes || []
        incompleteModes = data.incomplete_modes || []
        incompleteTokens = data.incomplete_tokens || []
        candidateModes = data.candidate_modes || []
        var preferredMode = requestedMode || data.default_mode
        var preferredTokenKey = requestedTokenKey
        activeMode = chooseMode(preferredMode, availableModes, resultsByMode)
        requestedMode = ""
        requestedTokenKey = ""
        showRelated = false
        segmented = data.segmented === true
        tokens = data.tokens || []
        tokenItems = normalizedTokenItems(data.token_items, tokens)
        resultsByToken = data.results_by_token || ({})
        activeToken = ""
        activeTokenKey = ""
        var tokenSelected = false
        if (segmented && preferredTokenKey !== "")
            tokenSelected = selectTokenKey(preferredTokenKey, preferredMode)
        if (segmented && !tokenSelected
                && !resultMapIsUsable(topResultsByMode, availableModes)) {
            for (var i = 0; i < tokenItems.length; ++i) {
                var item = tokenItems[i]
                var resultKey = tokenItemResultKey(item.key, item.token, item.index)
                if (resultKey !== "" && selectTokenKey(resultKey, preferredMode)) {
                    tokenSelected = true
                    break
                }
            }
        }
        backendTiming = data.timing || ({})
        errorText = responseErrorMessage(data.error)
        phase = errorText !== "" ? "error" : "ready"
        scrollResetRequested()
        if (benchmarkEnabled)
            pendingResultQmlFrameTick = true
    }

    // Tokens cut short by the lookup budget are named before bare modes, so a
    // segmented lookup never silently shows empty tokens.
    function incompleteLabel() {
        if (incompleteTokens.length > 0) {
            var names = []
            for (var i = 0; i < tokenItems.length; ++i) {
                if (incompleteTokens.indexOf(tokenItems[i].key) >= 0)
                    names.push(tokenItems[i].token)
            }
            if (names.length > 0)
                return "Timed out: " + names.join(", ")
        }
        return incompleteModes.length > 0
                ? "Timed out: " + incompleteModes.join(", ") : ""
    }

    function normalizedTokenItems(items, legacyTokens) {
        var normalized = []
        if (Array.isArray(items) && items.length > 0) {
            for (var i = 0; i < items.length; ++i) {
                var item = items[i] || ({})
                var index = item.index === undefined ? i : Number(item.index)
                var token = String(item.token === undefined ? "" : item.token)
                normalized.push({
                    key: item.key === undefined ? String(index) + ":" + token
                                                : String(item.key),
                    index: index,
                    token: token
                })
            }
            return normalized
        }

        var fallback = Array.isArray(legacyTokens) ? legacyTokens : []
        for (var j = 0; j < fallback.length; ++j) {
            var legacyToken = String(fallback[j])
            normalized.push({
                key: String(j) + ":" + legacyToken,
                index: j,
                token: legacyToken
            })
        }
        return normalized
    }

    function responseErrorMessage(error) {
        if (error === undefined || error === null)
            return ""
        if (typeof error === "string")
            return trimmed(error)
        if (typeof error === "object") {
            if (error.message !== undefined)
                return trimmed(error.message)
            if (error.code !== undefined)
                return "Lookup failed (" + trimmed(error.code) + ")"
        }
        return "Lookup failed"
    }

    function applyLocalMode(mode) {
        if (availableModes.indexOf(mode) < 0 || !resultsByMode[mode])
            return false
        activeMode = mode
        showRelated = false
        scrollResetRequested()
        return true
    }

    function applyMode(mode) {
        if (availableModes.indexOf(mode) < 0 || !resultsByMode[mode])
            return false
        return startFollowupLookup("mode_change", selectionSource, mode) !== ""
    }

    function setRelated(enabled) {
        var next = enabled === true && hasRelated()
        if (showRelated === next)
            return
        showRelated = next
        scrollResetRequested()
    }

    function toggleRelated() {
        setRelated(!showRelated)
    }

    function tokenResultKey(token, index) {
        var raw = String(token || "")
        if (raw === "")
            return ""
        if (tokenResultIsUsable(resultsByToken[raw]))
            return raw
        if (index !== undefined && index !== null && Number(index) >= 0) {
            var indexed = String(index) + ":" + raw
            if (tokenResultIsUsable(resultsByToken[indexed]))
                return indexed
        }
        var keys = Object.keys(resultsByToken)
        for (var i = 0; i < keys.length; ++i) {
            var value = resultsByToken[keys[i]]
            if (tokenResultIsUsable(value)
                    && String(value.token || "") === raw
                    && (index === undefined || index === null
                        || Number(index) < 0 || Number(value.index) === Number(index)))
                return keys[i]
        }
        // Legacy/token-only callers cannot disambiguate duplicates. UI callers
        // always pass the Repeater index and therefore select the exact key.
        for (var j = 0; j < keys.length; ++j) {
            if (keys[j].endsWith(":" + raw)
                    && tokenResultIsUsable(resultsByToken[keys[j]]))
                return keys[j]
        }
        return ""
    }

    function tokenResultIsUsable(tokenResult) {
        if (!tokenResult || typeof tokenResult !== "object")
            return false
        var exact = tokenResult.results_by_mode || ({})
        // An explicitly empty available_modes array is authoritative. Core may
        // retain an empty token entry for positional fidelity; it must not turn
        // into an enabled/selectable UI token merely because its key exists.
        var modes = Array.isArray(tokenResult.available_modes)
                ? tokenResult.available_modes : modeKeys(exact)
        return resultMapIsUsable(exact, modes)
    }

    function resultMapIsUsable(exact, modes) {
        for (var i = 0; i < modes.length; ++i) {
            var result = exact[modes[i]]
            if (result && resultBody(result) !== "")
                return true
        }
        return false
    }

    function tokenItemResultKey(key, token, index) {
        var declared = String(key || "")
        if (declared !== "" && tokenResultIsUsable(resultsByToken[declared]))
            return declared
        return tokenResultKey(token, index)
    }

    function hasTokenResult(token, index) {
        return tokenResultKey(token, index) !== ""
    }

    function selectToken(token, index, preferredMode) {
        var key = String(token || "")
        if (key === "") {
            activeToken = ""
            activeTokenKey = ""
            resultsByMode = topResultsByMode
            relatedByMode = topRelatedByMode
            availableModes = response.available_modes || modeKeys(resultsByMode)
            activeMode = chooseMode(response.default_mode, availableModes,
                                    resultsByMode)
            showRelated = false
            scrollResetRequested()
            return true
        }

        var resultKey = tokenResultKey(key, index)
        var tokenResult = resultKey !== "" ? resultsByToken[resultKey] : null
        if (!tokenResult)
            return false
        activeToken = key
        activeTokenKey = resultKey
        resultsByMode = tokenResult.results_by_mode || ({})
        relatedByMode = tokenResult.related_by_mode || ({})
        availableModes = tokenResult.available_modes || modeKeys(resultsByMode)
        activeMode = chooseMode(preferredMode || tokenResult.default_mode, availableModes,
                                resultsByMode)
        showRelated = false
        scrollResetRequested()
        return true
    }

    function selectTokenKey(key, preferredMode) {
        var result = resultsByToken[key]
        if (!tokenResultIsUsable(result))
            return false
        var separator = key.indexOf(":")
        var displayToken = result.token !== undefined ? String(result.token)
                                                     : (separator >= 0
                                                        ? key.slice(separator + 1)
                                                        : key)
        var index = result.index !== undefined ? Number(result.index)
                                               : (separator >= 0
                                                  ? Number(key.slice(0, separator))
                                                  : -1)
        return selectToken(displayToken, index, preferredMode)
    }

    function emptyStateMessage() {
        if (segmented && activeTokenKey === "")
            return "Whole phrase not found — choose a token"
        return "No exact definition found"
    }

    function activeResult() {
        if (showRelated && relatedByMode && relatedByMode[activeMode])
            return relatedByMode[activeMode]
        return resultsByMode && resultsByMode[activeMode]
                ? resultsByMode[activeMode] : ({})
    }

    function hasRelated() {
        return !!(activeMode && relatedByMode && relatedByMode[activeMode]
                  && resultBody(relatedByMode[activeMode]) !== "")
    }

    function entryText(entry) {
        if (!entry || typeof entry !== "object")
            return ""
        var lines = []
        if (entry.source)
            lines.push("[" + entry.source + "]")
        if (entry.word)
            lines.push(String(entry.word))
        var pronunciations = entry.pronunciations || entry.pronunciation || []
        if (!Array.isArray(pronunciations))
            pronunciations = [pronunciations]
        if (pronunciations.length > 0)
            lines.push(pronunciations.join(" · "))
        if (entry.body)
            lines.push(String(entry.body))
        var senses = entry.senses || []
        for (var i = 0; i < senses.length; ++i) {
            var sense = senses[i]
            if (typeof sense === "string")
                lines.push(sense)
            else if (sense && sense.definition)
                lines.push(String(sense.definition))
        }
        var examples = entry.examples || []
        for (var j = 0; j < examples.length; ++j)
            lines.push("• " + String(examples[j]))
        return lines.join("\n").trim()
    }

    function resultBody(result) {
        if (!result)
            return ""
        if (result.body !== undefined && String(result.body) !== "")
            return String(result.body).replace(/\n{3,}/g, "\n\n").trim()
        var entries = result.entries || []
        var parts = []
        for (var i = 0; i < entries.length; ++i) {
            var text = entryText(entries[i])
            if (text !== "")
                parts.push(text)
        }
        return parts.join("\n\n").trim()
    }

    function activeBody() {
        return resultBody(activeResult())
    }

    // ---- Display shaping ------------------------------------------------------
    // The card shows each entry under its own source label instead of the flat
    // body (which stays the copy/open payload). Headword echo lines (the word
    // repeated by the renderer and again by many dictionaries) are dropped, and
    // a leading pronunciation line of the first entry moves under the title.
    readonly property var pronunciationPattern:
        /^(?:音讀|讀音|注音|拼音|粵拼|粵音|发音|發音)\s*[：:]\s*(.+)$|^([\/\[][^\/\[\]]{1,60}[\/\]])$/

    // Lines are {text, runs}; runs are the backend's [text, flags] style runs
    // ("b" bold, "i" italic, "d" dimmed). A body without runs is one plain run
    // per line.
    function entryLines(entry) {
        var runs = Array.isArray(entry.runs) && entry.runs.length > 0
                   ? entry.runs : [[String(entry.body), ""]]
        var lines = [{text: "", runs: []}]
        for (var i = 0; i < runs.length; ++i) {
            var pieces = String(runs[i][0]).split("\n")
            for (var j = 0; j < pieces.length; ++j) {
                if (j > 0)
                    lines.push({text: "", runs: []})
                if (pieces[j] === "")
                    continue
                var line = lines[lines.length - 1]
                line.text += pieces[j]
                line.runs.push([pieces[j], String(runs[i][1] || "")])
            }
        }
        return lines
    }

    function stripEcho(lines, words) {
        var start = 0
        while (start < lines.length) {
            var line = lines[start].text.trim()
            if (line === "" || words.indexOf(line) >= 0)
                ++start
            else
                break
        }
        return lines.slice(start)
    }

    function escapeStyled(text) {
        return String(text).replace(/&/g, "&amp;").replace(/</g, "&lt;")
                .replace(/>/g, "&gt;").replace(/"/g, "&quot;")
                .replace(/^ /, "&nbsp;").replace(/ {2}/g, " &nbsp;")
    }

    // Qt StyledText from line runs. Every character is escaped; the only
    // markup is what this function writes.
    function styledMarkup(lines, dimColor) {
        var out = []
        for (var i = 0; i < lines.length; ++i) {
            var parts = []
            for (var j = 0; j < lines[i].runs.length; ++j) {
                var run = lines[i].runs[j]
                var piece = escapeStyled(run[0])
                var flags = run[1]
                if (flags.indexOf("i") >= 0)
                    piece = "<i>" + piece + "</i>"
                if (flags.indexOf("b") >= 0)
                    piece = "<b>" + piece + "</b>"
                if (flags.indexOf("d") >= 0)
                    piece = "<font color=\"" + dimColor + "\">" + piece + "</font>"
                parts.push(piece)
            }
            out.push(parts.join(""))
        }
        return out.join("<br>")
    }

    function displayFor(result, query) {
        var shaped = {pronunciation: "", sections: []}
        if (!result)
            return shaped
        var entries = result.entries || []
        var queryWord = String(query || "").trim()
        for (var i = 0; i < entries.length; ++i) {
            var entry = entries[i]
            if (!entry || entry.body === undefined || entry.body === null)
                continue
            var words = [queryWord, String(entry.word || "").trim()]
            var lines = stripEcho(entryLines(entry), words)
            if (shaped.sections.length === 0 && shaped.pronunciation === "") {
                // Character entries put radical and stroke lines first.
                for (var k = 0; k < Math.min(4, lines.length); ++k) {
                    var match = pronunciationPattern.exec(lines[k].text.trim())
                    // A bracketed line counts only without Han characters, so
                    // part-of-speech tags such as [動] stay in the body.
                    // Source notes such as [Webster 1913 Suppl.] are not readings.
                    if (match && match[2] && (/[\u3400-\u9fff]/.test(match[2])
                                              || /\d{3,}|Webster|Suppl|Etym/.test(match[2])))
                        match = null
                    if (match) {
                        shaped.pronunciation = (match[1] || match[2]).trim()
                        lines.splice(k, 1)
                        lines = stripEcho(lines, words)
                        break
                    }
                }
            }
            // Collapse runs of blank lines and drop trailing ones.
            var kept = []
            for (var m = 0; m < lines.length; ++m) {
                var blank = lines[m].text.trim() === ""
                if (blank && (kept.length === 0 || kept[kept.length - 1].text.trim() === ""))
                    continue
                kept.push(lines[m])
            }
            while (kept.length > 0 && kept[kept.length - 1].text.trim() === "")
                kept.pop()
            if (kept.length === 0)
                continue
            var plain = kept.map(function(line) { return line.text }).join("\n")
            shaped.sections.push({
                source: String(entry.source || ""),
                text: plain,
                styled: Array.isArray(entry.runs) && entry.runs.length > 0,
                lines: kept
            })
        }
        // Results without backend entries (older schema, mock backends) keep
        // the flat body as one unlabeled section.
        if (shaped.sections.length === 0 && shaped.pronunciation === "") {
            var body = resultBody(result)
            if (body !== "")
                shaped.sections.push({source: "", text: body, styled: false, lines: []})
        }
        return shaped
    }

    readonly property var activeDisplay: displayFor(activeResult(), queryText)

    function activeFullBody() {
        var result = activeResult()
        if (result && result.full_body !== undefined
                && result.full_body !== null
                && String(result.full_body) !== "")
            return String(result.full_body)
        // A ref means the complete body intentionally is not inline. Do not
        // silently substitute the preview: Open full must use open-ref.
        if (activeFullRef() !== "")
            return ""
        return activeBody()
    }

    function activeFullRef() {
        var result = activeResult()
        return result && result.full_ref !== undefined
                && result.full_ref !== null ? String(result.full_ref) : ""
    }

    function activeFullChars() {
        var result = activeResult()
        if (!result || result.full_chars === undefined
                || result.full_chars === null)
            return activeFullBody().length
        var count = Number(result.full_chars)
        return isFinite(count) && count >= 0 ? count : 0
    }

    function activeOpenSpec() {
        var ref = activeFullRef()
        if (ref !== "") {
            var refArgv = [backendCommand, "open-ref", "--request-id", requestId,
                           "--ref", ref]
            if (configPath !== "")
                refArgv.push("--config", configPath)
            return {
                kind: "ref",
                argv: refArgv,
                // Query is bounded and parsed by the helper from stdin. It is
                // never exposed through process argv or shell interpolation.
                payload: queryText
            }
        }
        var body = activeFullBody()
        if (body === "")
            return {kind: "none", argv: [], payload: ""}
        return {
            kind: "inline",
            argv: [backendCommand, "open-text", "--request-id", requestId],
            payload: body
        }
    }

    function activeSubtitle() {
        var result = activeResult()
        return result && result.subtitle ? String(result.subtitle) : ""
    }

    function activeLabel() {
        var result = activeResult()
        return result && result.label ? String(result.label) : activeMode
    }

    function activeTruncated() {
        var result = activeResult()
        return !!(result && result.truncated)
    }

    function noteFrame() {
        if (!benchmarkEnabled)
            return
        if (pendingFirstQmlFrameTick) {
            firstQmlFrameTickNs = requestTimer.elapsedNs()
            pendingFirstQmlFrameTick = false
        }
        if (pendingResultQmlFrameTick) {
            resultQmlFrameTickNs = requestTimer.elapsedNs()
            pendingResultQmlFrameTick = false
        }
    }

    function benchmarkSnapshot() {
        if (!benchmarkEnabled)
            return JSON.stringify({enabled: false})
        return JSON.stringify({
            enabled: true,
            sequence: sequence,
            phase: phase,
            lookup_started_ns: lookupStartedNs,
            first_qml_frame_tick_ns: firstQmlFrameTickNs,
            result_qml_frame_tick_ns: resultQmlFrameTickNs,
            offscreen: Quickshell.env("QT_QPA_PLATFORM") === "offscreen",
            proxy: true,
            backend_total_ms: backendTiming.total_ms === undefined
                              ? null : backendTiming.total_ms
        })
    }

    function injectMock(jsonText) {
        if (!mockIpcEnabled)
            return "mock-disabled"
        try {
            var data = JSON.parse(String(jsonText || ""))
            if (!data.request_id)
                data.request_id = requestId
            if (data.sequence === undefined)
                data.sequence = sequence
            if (data.query !== undefined)
                queryText = trimmed(data.query)
            applyResponse(data)
            showRequested()
            return "ok"
        } catch (error) {
            return "invalid-json"
        }
    }

    function runTestAction(action, argument) {
        if (!mockIpcEnabled)
            return "mock-disabled"
        if (action === "mode")
            return applyLocalMode(argument) ? "ok" : "unavailable"
        if (action === "related") {
            setRelated(argument === "true")
            return "ok"
        }
        if (action === "token")
            return selectToken(argument, -1) ? "ok" : "unavailable"
        if (action === "token-key")
            return selectTokenKey(argument) ? "ok" : "unavailable"
        if (action === "token-item") {
            var tokenItemIndex = Number(argument)
            if (!isFinite(tokenItemIndex) || tokenItemIndex < 0
                    || tokenItemIndex >= tokenItems.length)
                return "unavailable"
            return selectTokenKey(tokenItems[tokenItemIndex].key)
                    ? "ok" : "unavailable"
        }
        if (action === "snapshot") {
            return JSON.stringify({
                phase: phase,
                error: errorText,
                mode: activeMode,
                token: activeToken,
                token_key: activeTokenKey,
                related: showRelated,
                body: activeBody(),
                full_body: activeFullBody(),
                full_ref: activeFullRef(),
                full_chars: activeFullChars(),
                truncated: activeTruncated(),
                empty_message: emptyStateMessage(),
                incomplete_label: incompleteLabel()
            })
        }
        if (action === "meta") {
            return JSON.stringify({
                mode: activeMode,
                token: activeToken,
                token_key: activeTokenKey,
                related: showRelated,
                body_length: activeBody().length,
                full_body_length: activeFullBody().length,
                full_ref: activeFullRef(),
                full_chars: activeFullChars(),
                truncated: activeTruncated(),
                available_mode_count: availableModes.length,
                token_item_count: tokenItems.length
            })
        }
        if (action === "open-spec") {
            var spec = activeOpenSpec()
            return JSON.stringify({
                kind: spec.kind,
                argv: spec.argv,
                stdin_chars: spec.payload.length
            })
        }
        if (action === "copy-payload")
            return activeBody()
        if (action === "copy") {
            testCopyRequested()
            return "started"
        }
        if (action === "theme")
            return JSON.stringify(testThemeSnapshot)
        if (action === "finish-fixture") {
            try {
                var fixture = JSON.parse(String(argument || ""))
                var fixtureResponse = fixture.response || ({})
                finishLookup(Number(fixture.sequence),
                             String(fixture.request_id || ""),
                             Number(fixture.exit_code || 0),
                             JSON.stringify(fixtureResponse),
                             String(fixture.stderr || ""))
                return "ok"
            } catch (error) {
                return "invalid-json"
            }
        }
        if (action === "open") {
            testOpenRequested()
            return "started"
        }
        return "unknown-action"
    }

    IpcHandler {
        target: root.ipcTarget

        function prepare(source: string): string {
            return root.prepareRequest(source)
        }

        function capturePrepared(correlationId: string, source: string): string {
            return root.startPreparedCapture(correlationId, source)
        }

        function relookup(source: string, mode: string): string {
            return root.relookup(source, mode) ? root.requestId : "unavailable"
        }

        function changeSource(source: string): string {
            return root.changeSource(source) ? root.requestId : "unavailable"
        }

        function changeMode(mode: string): string {
            return root.applyMode(mode) ? root.requestId : "unavailable"
        }

        function close(): string {
            root.closeRequested()
            return "closed"
        }

        function status(): string {
            return JSON.stringify({
                phase: root.phase,
                sequence: root.sequence,
                visible: root.windowVisible,
                selection_status: root.captureStatus,
                selection_source: root.selectionSource,
                primary_status: root.primaryCaptureStatus,
                clipboard_status: root.clipboardCaptureStatus
            })
        }

        function benchmarkState(): string {
            return root.benchmarkSnapshot()
        }

        function mockResult(jsonText: string): string {
            return root.injectMock(jsonText)
        }

        function testAction(action: string, argument: string): string {
            return root.runTestAction(action, argument)
        }
    }

    Component {
        id: lookupProcess

        Process {
            property int requestSequence: 0
            property string correlationId: ""
            property string payload: ""
            property bool transportRequest: true
            stdinEnabled: true

            stdout: StdioCollector {
                id: stdoutCollector
                waitForEnd: true
            }

            stderr: StdioCollector {
                id: stderrCollector
                waitForEnd: true
            }


            onStarted: {
                write(payload)
                stdinEnabled = false
            }

            onExited: function(exitCode, exitStatus) {
                root.finishLookup(requestSequence, correlationId, exitCode,
                                  stdoutCollector.text, stderrCollector.text)
                destroy()
            }
        }
    }

    Component {
        id: selectionCaptureProcess

        Process {
            property int requestSequence: 0
            property string correlationId: ""
            property string captureSource: "selection"
            property bool transportRequest: false

            stdout: StdioCollector {
                id: captureStdout
                waitForEnd: true
            }

            onExited: function(exitCode, exitStatus) {
                root.finishCapture(requestSequence, correlationId, captureSource,
                                   exitCode, captureStdout.text)
                destroy()
            }
        }
    }

    Component {
        id: cancelProcess

        Process {
            property string payload: ""
            stdinEnabled: true

            onStarted: {
                write(payload)
                stdinEnabled = false
            }

            onExited: function(exitCode, exitStatus) {
                destroy()
            }
        }
    }
}
