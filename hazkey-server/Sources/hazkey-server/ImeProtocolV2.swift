import Foundation

/// Codable representation used by the process/E2E harness and by the socket
/// adapter while SwiftProtobuf generated sources are refreshed.  The wire
/// schema is declared in protocol/base.proto and protocol/commands.proto;
/// keeping this value type separate also makes contract tests independent of
/// the dictionary and the protobuf runtime.
struct ImeV2Request: Equatable, Codable, Sendable {
    let requestID: String
    let expectedRevision: UInt64
    let action: ImeAction
}

struct ImeV2Response: Equatable, Codable, Sendable {
    let status: ImeReductionStatus
    let message: String?
    let snapshot: SessionSnapshot
}

struct ImeV2Negotiation: Equatable, Codable, Sendable {
    static let protocolVersion: UInt32 = 2
    static let snapshotVersion: UInt32 = 1

    let featureBits: UInt64
    let recoverySupport: Bool
    let idempotentRequestSupport: Bool

    static let current = ImeV2Negotiation(
        featureBits: 0b1111,
        recoverySupport: true,
        idempotentRequestSupport: true
    )
}

final class ImeV2SessionController {
    let negotiation: ImeV2Negotiation
    private let reducer: ImeReducer
    private let policyProvider: (() -> PinnedCompositionPolicy)?

    init(
        reducer: ImeReducer = ImeReducer(),
        negotiation: ImeV2Negotiation = .current,
        policyProvider: (() -> PinnedCompositionPolicy)? = nil
    ) {
        self.reducer = reducer
        self.negotiation = negotiation
        self.policyProvider = policyProvider
    }

    var snapshot: SessionSnapshot {
        reducer.currentSnapshot()
    }

    func invalidateForDictionaryChange() {
        reducer.invalidateCandidatesForExternalDictionaryChange()
    }

    func handle(_ request: ImeV2Request) -> ImeV2Response {
        guard !request.requestID.isEmpty,
              request.requestID.utf8.count <= 128 else {
            return ImeV2Response(
                status: .invalidAction,
                message: "request_id must contain 1...128 UTF-8 bytes",
                snapshot: snapshot
            )
        }
        prepareCompositionIfNeeded(for: request.action)
        let result = reducer.reduce(
            request.action,
            requestID: request.requestID,
            expectedRevision: request.expectedRevision
        )
        return ImeV2Response(
            status: result.status,
            message: result.message,
            snapshot: result.snapshot
        )
    }

    private func prepareCompositionIfNeeded(for action: ImeAction) {
        let beginsComposition: Bool
        switch action {
        case .insertText, .reconvert, .beginUnicodeInput:
            beginsComposition = true
        default:
            beginsComposition = false
        }
        guard beginsComposition,
              reducer.session.phase == .idle,
              reducer.session.composingText.isEmpty,
              let policyProvider else { return }
        reducer.pinCompositionPolicy(policyProvider())
    }

    func handle(_ request: Hazkey_Commands_HandleImeAction) -> Hazkey_ResponseEnvelope {
        guard let action = semanticAction(for: request) else {
            return Hazkey_ResponseEnvelope.with {
                $0.status = .malformedRequest
                $0.errorMessage = "HandleImeAction does not contain a supported action"
                $0.handleImeActionResult = Hazkey_HandleImeActionResult.with {
                    $0.status = .malformedRequest
                    $0.errorMessage = "HandleImeAction does not contain a supported action"
                    $0.snapshot = protobufSnapshot(snapshot)
                }
            }
        }
        let result = handle(ImeV2Request(
            requestID: request.requestID,
            expectedRevision: request.expectedRevision,
            action: action
        ))
        return protobufResponse(result)
    }

    func recover(from checkpoint: RecoveryCheckpoint) -> ImeV2Response {
        guard !reducer.session.policy.secureInput else {
            return ImeV2Response(
                status: .secureInputViolation,
                message: "secure input sessions cannot restore checkpoints",
                snapshot: reducer.reduce(.cancel, requestID: "__secure-recovery__").snapshot
            )
        }
        guard let data = try? checkpoint.persistedData(isSecureInput: false) else {
            return ImeV2Response(
                status: .invalidAction,
                message: "checkpoint could not be encoded",
                snapshot: snapshot
            )
        }
        return handle(ImeV2Request(
            requestID: "__recovery-\(checkpoint.revision)__",
            expectedRevision: reducer.session.revision,
            action: .restoreCheckpoint(data)
        ))
    }

    private func semanticAction(
        for request: Hazkey_Commands_HandleImeAction
    ) -> ImeAction? {
        switch request.action {
        case .insertText(let value): return .insertText(value.text)
        case .deleteBackward: return .deleteBackward
        case .deleteForward: return .deleteForward
        case .moveCursorV2(let value): return .moveCursor(Int(value.offset))
        case .moveCursorToEdge(let value):
            switch value.edge {
            case .start: return .moveCursorToStart
            case .end: return .moveCursorToEnd
            default: return nil
            }
        case .startConversion: return .startConversion
        case .navigateCandidate(let value): return .navigateCandidate(Int(value.delta))
        case .navigateCandidatePage(let value):
            return .navigateCandidatePage(Int(value.delta))
        case .resizeSegment(let value): return .resizeSegment(Int(value.delta))
        case .moveActiveSegment(let value):
            return .moveActiveSegment(Int(value.offset))
        case .commitSelected: return .commitSelected
        case .commitAll: return .commitAll
        case .cancel: return .cancel
        case .selectCandidate(let value):
            return .selectCandidate(id: value.candidateID, generation: value.generation)
        case .transformActiveSegment(let value):
            switch value.transform {
            case .hiragana: return .transformActiveSegment(.hiragana)
            case .katakanaFullwidth: return .transformActiveSegment(.katakanaFullwidth)
            case .katakanaHalfwidth: return .transformActiveSegment(.katakanaHalfwidth)
            case .alphabetFullwidth: return .transformActiveSegment(.alphabetFullwidth)
            case .alphabetHalfwidth: return .transformActiveSegment(.alphabetHalfwidth)
            default: return nil
            }
        case .lifecycleEvent(let value):
            switch value.event {
            case .deactivate: return .lifecycle(.deactivate)
            case .focusChanged: return .lifecycle(.focusChanged)
            case .capabilityChanged:
                return .lifecycle(.capabilityChanged(
                    clientPreedit: value.hasClientPreedit ? value.clientPreedit : false
                ))
            case .secureInputChanged:
                guard value.hasSecureInput else { return nil }
                return .lifecycle(.secureInputChanged(value.secureInput))
            case .serverRestarted: return .lifecycle(.serverRestarted)
            default: return nil
            }
        case .forgetCandidate(let value):
            return .forgetCandidate(id: value.candidateID, generation: value.generation)
        case .reconvert(let value):
            return .reconvert(
                text: value.text,
                leftContext: value.leftContext,
                rightContext: value.rightContext,
                deleteBefore: Int(value.deleteBefore),
                deleteAfter: Int(value.deleteAfter)
            )
        case .updateSurroundingContext(let value):
            let scalars = value.text.unicodeScalars
            let anchor = Int(value.anchor)
            guard anchor >= 0, anchor <= scalars.count else { return nil }
            let index = scalars.index(scalars.startIndex, offsetBy: anchor)
            return .updateContext(
                leftContext: String(scalars[..<index]),
                rightContext: String(scalars[index...])
            )
        case .restoreCheckpoint(let value):
            return .restoreCheckpoint(value.opaqueState)
        case .beginUnicodeInput:
            return .beginUnicodeInput
        case .appendUnicodeDigit(let value):
            return .appendUnicodeDigit(value.digit)
        case .commitUnicodeInput:
            return .commitUnicodeInput
        case nil: return nil
        }
    }
}

private extension ImeReductionStatus {
    var protobufStatus: Hazkey_StatusCode {
        switch self {
        case .success: return .success
        case .staleRevision: return .staleRevision
        case .staleCandidate: return .staleCandidate
        case .invalidAction: return .invalidAction
        case .converterUnavailable: return .converterUnavailable
        case .secureInputViolation: return .secureInputViolation
        }
    }
}

private func protobufResponse(_ response: ImeV2Response) -> Hazkey_ResponseEnvelope {
    Hazkey_ResponseEnvelope.with {
        $0.status = response.status.protobufStatus
        $0.errorMessage = response.message ?? ""
        $0.handleImeActionResult = Hazkey_HandleImeActionResult.with {
            $0.status = response.status.protobufStatus
            $0.errorMessage = response.message ?? ""
            $0.snapshot = protobufSnapshot(response.snapshot)
        }
    }
}

private func protobufSnapshot(_ snapshot: SessionSnapshot) -> Hazkey_SessionSnapshot {
    Hazkey_SessionSnapshot.with {
        $0.revision = snapshot.revision
        $0.phase = switch snapshot.phase {
        case .idle: .idle
        case .composing: .composing
        case .previewing: .previewing
        case .selecting: .selecting
        case .reconverting: .reconverting
        case .unicodeInput: .unicodeInput
        }
        $0.preedit = snapshot.preedit.map { span in
            Hazkey_PreeditSpan.with {
                $0.text = span.text
                $0.style = switch span.style {
                case .plain: .plain
                case .underline: .underline
                case .active: .active
                }
            }
        }
        if let caret = snapshot.caretUtf8ByteOffset {
            $0.caretUtf8ByteOffset = caret
        }
        $0.candidateWindow = Hazkey_CandidateWindowSnapshot.with {
            $0.generation = snapshot.candidateWindow.generation
            $0.items = snapshot.candidateWindow.items.map { item in
                Hazkey_CandidateSnapshot.with {
                    $0.id = item.id
                    $0.text = item.text
                    $0.annotation = item.annotation ?? ""
                    $0.consumingCount = UInt32(max(item.consumingCount, 0))
                }
            }
            if let selected = snapshot.candidateWindow.selectedIndex {
                $0.selectedIndex = UInt32(max(selected, 0))
            }
            $0.pageSize = UInt32(max(snapshot.candidateWindow.pageSize, 0))
        }
        $0.effects = snapshot.effects.map { effect in
            Hazkey_ClientEffect.with {
                switch effect {
                case .commitText(let effectID, let text):
                    $0.effectID = effectID
                    $0.type = .commitText
                    $0.text = text
                case .deleteSurroundingText(let effectID, let before, let after):
                    $0.effectID = effectID
                    $0.type = .deleteSurroundingText
                    $0.before = Int32(before)
                    $0.after = Int32(after)
                case .switchInputMode(let effectID, let mode):
                    $0.effectID = effectID
                    $0.type = .switchInputMode
                    $0.mode = mode
                case .notify(let effectID, let message):
                    $0.effectID = effectID
                    $0.type = .notify
                    $0.message = message
                }
            }
        }
        if let recovery = snapshot.recovery,
           let data = try? recovery.persistedData(isSecureInput: false) {
            $0.recovery = Hazkey_RecoveryCheckpoint.with {
                $0.revision = recovery.revision
                $0.opaqueState = data
            }
        }
        $0.aux = snapshot.aux ?? ""
    }
}
