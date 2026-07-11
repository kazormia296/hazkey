import Foundation

struct GrimodexRuntimeDiagnostics: Equatable, Sendable {
    let watcherActive: Bool
    let consumerRegistered: Bool
    let snapshot: GrimodexPublishedSnapshot
}

struct GrimodexSessionDiagnostics: Equatable, Sendable {
    let activeSessions: Int
    let clientContext: GrimodexClientContext?
    let scopeDecision: GrimodexScopeDecision?
}

struct GrimodexDiagnosticsSnapshot: Equatable, Sendable {
    let watcherActive: Bool
    let consumerRegistered: Bool
    let loadDiagnostic: GrimodexLoadDiagnostic
    let generation: UInt64
    let activeProjectID: String?
    let activeSessions: Int
    let clientContext: GrimodexClientContext?
    let scopeDecision: GrimodexScopeDecision?

    init(
        watcherActive: Bool,
        consumerRegistered: Bool,
        loadDiagnostic: GrimodexLoadDiagnostic,
        generation: UInt64,
        activeProjectID: String?,
        activeSessions: Int,
        clientContext: GrimodexClientContext?,
        scopeDecision: GrimodexScopeDecision?
    ) {
        self.watcherActive = watcherActive
        self.consumerRegistered = consumerRegistered
        self.loadDiagnostic = loadDiagnostic
        self.generation = generation
        self.activeProjectID = activeProjectID
        self.activeSessions = activeSessions
        self.clientContext = clientContext
        self.scopeDecision = scopeDecision
    }

    init(runtime: GrimodexRuntimeDiagnostics, sessions: GrimodexSessionDiagnostics) {
        self.init(
            watcherActive: runtime.watcherActive,
            consumerRegistered: runtime.consumerRegistered,
            loadDiagnostic: runtime.snapshot.diagnostic,
            generation: runtime.snapshot.generation,
            activeProjectID: runtime.snapshot.payload?.projectID,
            activeSessions: sessions.activeSessions,
            clientContext: sessions.clientContext,
            scopeDecision: sessions.scopeDecision
        )
    }

    static let unavailable = GrimodexDiagnosticsSnapshot(
        watcherActive: false,
        consumerRegistered: false,
        loadDiagnostic: .missingState,
        generation: 0,
        activeProjectID: nil,
        activeSessions: 0,
        clientContext: nil,
        scopeDecision: nil
    )

    var protobuf: Hazkey_Config_GrimodexDiagnostics {
        Hazkey_Config_GrimodexDiagnostics.with {
            $0.watcherActive = watcherActive
            $0.consumerRegistered = consumerRegistered
            $0.snapshotStatus = loadDiagnostic.rawValue
            $0.generation = generation
            if let activeProjectID { $0.activeProjectID = activeProjectID }
            $0.activeSessions = UInt32(clamping: activeSessions)
            if let clientContext {
                $0.program = clientContext.program
                $0.frontend = clientContext.frontend
                $0.secureInput = clientContext.secureInput
            }
            if let scopeDecision {
                $0.integrationAllowed = scopeDecision.allowsGrimodexIntegration
                $0.scopeReason = scopeDecision.reason.protobuf
            }
        }
    }
}

extension GrimodexScopeReason {
    var protobuf: Hazkey_Config_GrimodexDiagnostics.ScopeReason {
        switch self {
        case .allowedGrimodex: .allowedGrimodex
        case .allowedAllApplications: .allowedAllApplications
        case .disabled: .disabled
        case .secureInput: .secureInput
        case .unknownProgram: .unknownProgram
        case .otherProgram: .otherProgram
        }
    }
}
