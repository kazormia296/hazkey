import Foundation

enum PreeditStyle: String, Codable, Sendable {
    case plain
    case underline
    case active
}

struct PreeditSpan: Equatable, Codable, Sendable {
    let text: String
    let style: PreeditStyle
}

struct CandidateSnapshot: Equatable, Hashable, Codable, Sendable {
    let id: String
    let text: String
    let annotation: String?
    let consumingCount: Int
    /// Process-local identity of the converter candidate. It is deliberately
    /// omitted from Codable/protobuf snapshots and never crosses the client
    /// boundary.
    let sourceID: String?

    init(
        id: String,
        text: String,
        annotation: String? = nil,
        consumingCount: Int,
        sourceID: String? = nil
    ) {
        self.id = id
        self.text = text
        self.annotation = annotation
        self.consumingCount = consumingCount
        self.sourceID = sourceID
    }

    private enum CodingKeys: String, CodingKey {
        case id
        case text
        case annotation
        case consumingCount
    }

    init(from decoder: Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        self.init(
            id: try container.decode(String.self, forKey: .id),
            text: try container.decode(String.self, forKey: .text),
            annotation: try container.decodeIfPresent(
                String.self,
                forKey: .annotation
            ),
            consumingCount: try container.decode(Int.self, forKey: .consumingCount)
        )
    }
}

struct CandidateWindowSnapshot: Equatable, Codable, Sendable {
    let generation: UInt64
    let items: [CandidateSnapshot]
    let selectedIndex: Int?
    let pageSize: Int

    static let empty = CandidateWindowSnapshot(
        generation: 0, items: [], selectedIndex: nil, pageSize: 0
    )
}

enum ClientEffect: Equatable, Codable, Sendable {
    case commitText(effectID: UInt64, text: String)
    case deleteSurroundingText(effectID: UInt64, before: Int, after: Int)
    case switchInputMode(effectID: UInt64, mode: String)
    case notify(effectID: UInt64, message: String)
}

struct SessionSnapshot: Equatable, Codable, Sendable {
    let revision: UInt64
    let phase: ImePhase
    let preedit: [PreeditSpan]
    let caretUtf8ByteOffset: UInt32?
    let candidateWindow: CandidateWindowSnapshot
    let aux: String?
    let recovery: RecoveryCheckpoint?
    let effects: [ClientEffect]
}

enum ImeReductionStatus: String, Codable, Equatable, Sendable {
    case success
    case staleRevision
    case staleCandidate
    case invalidAction
    case converterUnavailable
    case secureInputViolation
}

struct ImeReductionResult: Equatable, Sendable {
    let status: ImeReductionStatus
    let message: String?
    let snapshot: SessionSnapshot
}
