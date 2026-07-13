import Foundation

enum ImeTextTransform: String, Codable, Sendable {
    case hiragana
    case katakanaFullwidth
    case katakanaHalfwidth
    case alphabetFullwidth
    case alphabetHalfwidth
}

enum ImeLifecycleEvent: Equatable, Codable, Sendable {
    case deactivate
    case focusChanged
    case capabilityChanged(clientPreedit: Bool)
    case secureInputChanged(Bool)
    case serverRestarted
}

enum ImeAction: Equatable, Codable, Sendable {
    case insertText(String)
    case deleteBackward
    case deleteForward
    case moveCursor(Int)
    case moveCursorToStart
    case moveCursorToEnd
    case startConversion
    case navigateCandidate(Int)
    case navigateCandidatePage(Int)
    case resizeSegment(Int)
    case commitSelected
    case commitAll
    case cancel
    case selectCandidate(id: String, generation: UInt64)
    case transformActiveSegment(ImeTextTransform)
    case forgetCandidate(id: String, generation: UInt64)
    case reconvert(
        text: String,
        leftContext: String,
        rightContext: String,
        deleteBefore: Int,
        deleteAfter: Int
    )
    case beginUnicodeInput
    case appendUnicodeDigit(String)
    case commitUnicodeInput
    case updateContext(leftContext: String, rightContext: String)
    case restoreCheckpoint(Data)
    case lifecycle(ImeLifecycleEvent)
}

extension ImeLifecycleEvent {
    private enum CodingKeys: String, CodingKey {
        case type
        case clientPreedit
        case secureInput
    }

    private enum Kind: String, Codable {
        case deactivate
        case focusChanged
        case capabilityChanged
        case secureInputChanged
        case serverRestarted
    }

    init(from decoder: Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        switch try container.decode(Kind.self, forKey: .type) {
        case .deactivate: self = .deactivate
        case .focusChanged: self = .focusChanged
        case .capabilityChanged:
            self = .capabilityChanged(
                clientPreedit: try container.decode(Bool.self, forKey: .clientPreedit)
            )
        case .secureInputChanged:
            self = .secureInputChanged(
                try container.decode(Bool.self, forKey: .secureInput)
            )
        case .serverRestarted: self = .serverRestarted
        }
    }

    func encode(to encoder: Encoder) throws {
        var container = encoder.container(keyedBy: CodingKeys.self)
        switch self {
        case .deactivate: try container.encode(Kind.deactivate, forKey: .type)
        case .focusChanged: try container.encode(Kind.focusChanged, forKey: .type)
        case .capabilityChanged(let clientPreedit):
            try container.encode(Kind.capabilityChanged, forKey: .type)
            try container.encode(clientPreedit, forKey: .clientPreedit)
        case .secureInputChanged(let secureInput):
            try container.encode(Kind.secureInputChanged, forKey: .type)
            try container.encode(secureInput, forKey: .secureInput)
        case .serverRestarted: try container.encode(Kind.serverRestarted, forKey: .type)
        }
    }
}

extension ImeAction {
    private enum CodingKeys: String, CodingKey {
        case type
        case text
        case offset
        case id
        case generation
        case transform
        case lifecycle
        case leftContext
        case rightContext
        case deleteBefore
        case deleteAfter
        case data
    }

    private enum Kind: String, Codable {
        case insertText
        case deleteBackward
        case deleteForward
        case moveCursor
        case moveCursorToStart
        case moveCursorToEnd
        case startConversion
        case navigateCandidate
        case navigateCandidatePage
        case resizeSegment
        case commitSelected
        case commitAll
        case cancel
        case selectCandidate
        case transformActiveSegment
        case forgetCandidate
        case reconvert
        case beginUnicodeInput
        case appendUnicodeDigit
        case commitUnicodeInput
        case updateContext
        case restoreCheckpoint
        case lifecycle
    }

    init(from decoder: Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        switch try container.decode(Kind.self, forKey: .type) {
        case .insertText: self = .insertText(try container.decode(String.self, forKey: .text))
        case .deleteBackward: self = .deleteBackward
        case .deleteForward: self = .deleteForward
        case .moveCursor: self = .moveCursor(try container.decode(Int.self, forKey: .offset))
        case .moveCursorToStart: self = .moveCursorToStart
        case .moveCursorToEnd: self = .moveCursorToEnd
        case .startConversion: self = .startConversion
        case .navigateCandidate: self = .navigateCandidate(try container.decode(Int.self, forKey: .offset))
        case .navigateCandidatePage: self = .navigateCandidatePage(try container.decode(Int.self, forKey: .offset))
        case .resizeSegment: self = .resizeSegment(try container.decode(Int.self, forKey: .offset))
        case .commitSelected: self = .commitSelected
        case .commitAll: self = .commitAll
        case .cancel: self = .cancel
        case .selectCandidate:
            self = .selectCandidate(
                id: try container.decode(String.self, forKey: .id),
                generation: try container.decode(UInt64.self, forKey: .generation)
            )
        case .transformActiveSegment:
            self = .transformActiveSegment(
                try container.decode(ImeTextTransform.self, forKey: .transform)
            )
        case .forgetCandidate:
            self = .forgetCandidate(
                id: try container.decode(String.self, forKey: .id),
                generation: try container.decode(UInt64.self, forKey: .generation)
            )
        case .reconvert:
            self = .reconvert(
                text: try container.decode(String.self, forKey: .text),
                leftContext: try container.decode(String.self, forKey: .leftContext),
                rightContext: try container.decode(String.self, forKey: .rightContext),
                deleteBefore: try container.decodeIfPresent(
                    Int.self,
                    forKey: .deleteBefore
                ) ?? 0,
                deleteAfter: try container.decodeIfPresent(
                    Int.self,
                    forKey: .deleteAfter
                ) ?? 0
            )
        case .beginUnicodeInput:
            self = .beginUnicodeInput
        case .appendUnicodeDigit:
            self = .appendUnicodeDigit(
                try container.decode(String.self, forKey: .text)
            )
        case .commitUnicodeInput:
            self = .commitUnicodeInput
        case .updateContext:
            self = .updateContext(
                leftContext: try container.decode(String.self, forKey: .leftContext),
                rightContext: try container.decode(String.self, forKey: .rightContext)
            )
        case .restoreCheckpoint:
            self = .restoreCheckpoint(
                try container.decode(Data.self, forKey: .data)
            )
        case .lifecycle:
            self = .lifecycle(try container.decode(ImeLifecycleEvent.self, forKey: .lifecycle))
        }
    }

    func encode(to encoder: Encoder) throws {
        var container = encoder.container(keyedBy: CodingKeys.self)
        func encode(_ kind: Kind) throws { try container.encode(kind, forKey: .type) }
        switch self {
        case .insertText(let text): try encode(.insertText); try container.encode(text, forKey: .text)
        case .deleteBackward: try encode(.deleteBackward)
        case .deleteForward: try encode(.deleteForward)
        case .moveCursor(let offset): try encode(.moveCursor); try container.encode(offset, forKey: .offset)
        case .moveCursorToStart: try encode(.moveCursorToStart)
        case .moveCursorToEnd: try encode(.moveCursorToEnd)
        case .startConversion: try encode(.startConversion)
        case .navigateCandidate(let delta): try encode(.navigateCandidate); try container.encode(delta, forKey: .offset)
        case .navigateCandidatePage(let delta): try encode(.navigateCandidatePage); try container.encode(delta, forKey: .offset)
        case .resizeSegment(let delta): try encode(.resizeSegment); try container.encode(delta, forKey: .offset)
        case .commitSelected: try encode(.commitSelected)
        case .commitAll: try encode(.commitAll)
        case .cancel: try encode(.cancel)
        case .selectCandidate(let id, let generation):
            try encode(.selectCandidate); try container.encode(id, forKey: .id); try container.encode(generation, forKey: .generation)
        case .transformActiveSegment(let transform):
            try encode(.transformActiveSegment); try container.encode(transform, forKey: .transform)
        case .forgetCandidate(let id, let generation):
            try encode(.forgetCandidate); try container.encode(id, forKey: .id); try container.encode(generation, forKey: .generation)
        case .reconvert(
            let text,
            let leftContext,
            let rightContext,
            let deleteBefore,
            let deleteAfter
        ):
            try encode(.reconvert)
            try container.encode(text, forKey: .text)
            try container.encode(leftContext, forKey: .leftContext)
            try container.encode(rightContext, forKey: .rightContext)
            try container.encode(deleteBefore, forKey: .deleteBefore)
            try container.encode(deleteAfter, forKey: .deleteAfter)
        case .beginUnicodeInput:
            try encode(.beginUnicodeInput)
        case .appendUnicodeDigit(let digit):
            try encode(.appendUnicodeDigit)
            try container.encode(digit, forKey: .text)
        case .commitUnicodeInput:
            try encode(.commitUnicodeInput)
        case .updateContext(let leftContext, let rightContext):
            try encode(.updateContext)
            try container.encode(leftContext, forKey: .leftContext)
            try container.encode(rightContext, forKey: .rightContext)
        case .restoreCheckpoint(let data):
            try encode(.restoreCheckpoint)
            try container.encode(data, forKey: .data)
        case .lifecycle(let lifecycle): try encode(.lifecycle); try container.encode(lifecycle, forKey: .lifecycle)
        }
    }
}
