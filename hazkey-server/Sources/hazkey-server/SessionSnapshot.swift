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
    /// Internal converter provenance. It is deliberately omitted from the
    /// wire snapshot, just like sourceID.
    let provenance: CandidateProvenance

    init(
        id: String,
        text: String,
        annotation: String? = nil,
        consumingCount: Int,
        sourceID: String? = nil,
        provenance: CandidateProvenance = .unknown
    ) {
        self.id = id
        self.text = text
        self.annotation = annotation
        self.consumingCount = consumingCount
        self.sourceID = sourceID
        self.provenance = provenance
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
            consumingCount: try container.decode(Int.self, forKey: .consumingCount),
            provenance: .unknown
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
    case scheduleLiveConversion(
        effectID: UInt64,
        delayMilliseconds: UInt32,
        scheduledRevision: UInt64
    )
}

struct SessionSnapshot: Equatable, Codable, Sendable {
    let revision: UInt64
    let phase: ImePhase
    let preedit: [PreeditSpan]
    let caretUtf8ByteOffset: UInt32?
    let candidateWindow: CandidateWindowSnapshot
    let aux: String?
    let pendingLearning: Bool
    let recovery: RecoveryCheckpoint?
    let effects: [ClientEffect]

    private enum CodingKeys: String, CodingKey {
        case revision
        case phase
        case preedit
        case caretUtf8ByteOffset
        case candidateWindow
        case aux
        case pendingLearning
        case recovery
        case effects
    }

    init(
        revision: UInt64,
        phase: ImePhase,
        preedit: [PreeditSpan],
        caretUtf8ByteOffset: UInt32?,
        candidateWindow: CandidateWindowSnapshot,
        aux: String?,
        pendingLearning: Bool,
        recovery: RecoveryCheckpoint?,
        effects: [ClientEffect]
    ) {
        self.revision = revision
        self.phase = phase
        self.preedit = preedit
        self.caretUtf8ByteOffset = caretUtf8ByteOffset
        self.candidateWindow = candidateWindow
        self.aux = aux
        self.pendingLearning = pendingLearning
        self.recovery = recovery
        self.effects = effects
    }

    init(from decoder: Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        revision = try container.decode(UInt64.self, forKey: .revision)
        phase = try container.decode(ImePhase.self, forKey: .phase)
        preedit = try container.decode([PreeditSpan].self, forKey: .preedit)
        caretUtf8ByteOffset = try container.decodeIfPresent(
            UInt32.self,
            forKey: .caretUtf8ByteOffset
        )
        candidateWindow = try container.decode(
            CandidateWindowSnapshot.self,
            forKey: .candidateWindow
        )
        aux = try container.decodeIfPresent(String.self, forKey: .aux)
        // Older local snapshots predate the pending-learning indicator. A
        // missing field means no staged transaction, preserving Codable
        // compatibility while the protobuf field remains additive.
        pendingLearning = try container.decodeIfPresent(
            Bool.self,
            forKey: .pendingLearning
        ) ?? false
        recovery = try container.decodeIfPresent(
            RecoveryCheckpoint.self,
            forKey: .recovery
        )
        effects = try container.decode([ClientEffect].self, forKey: .effects)
    }
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
