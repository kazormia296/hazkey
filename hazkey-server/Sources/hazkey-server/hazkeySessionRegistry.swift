import Foundation

protocol GrimodexSnapshotProviding: Sendable {
    func latest() -> GrimodexPublishedSnapshot
}

extension GrimodexSnapshotManager: GrimodexSnapshotProviding {}

protocol GrimodexScopeModeProviding: Sendable {
    func current() -> GrimodexScopeMode
}

private struct GrimodexFixedScopeModeProvider: GrimodexScopeModeProviding, Sendable {
    let value: GrimodexScopeMode

    func current() -> GrimodexScopeMode { value }
}

final class GrimodexScopeModeStore: GrimodexScopeModeProviding, @unchecked Sendable {
    private let lock = NSLock()
    private var value: GrimodexScopeMode

    init(_ value: GrimodexScopeMode) {
        self.value = value
    }

    func current() -> GrimodexScopeMode {
        lock.lock()
        defer { lock.unlock() }
        return value
    }

    func update(_ value: GrimodexScopeMode) {
        lock.lock()
        self.value = value
        lock.unlock()
    }
}

struct GrimodexSessionRevisionProvider: GrimodexRevisionProviding, Sendable {
    private let snapshotProvider: any GrimodexSnapshotProviding
    private let scopeModeProvider: any GrimodexScopeModeProviding
    private let clientContext: GrimodexClientContext

    init(
        snapshotProvider: any GrimodexSnapshotProviding,
        scopeMode: GrimodexScopeMode,
        clientContext: GrimodexClientContext
    ) {
        self.snapshotProvider = snapshotProvider
        self.scopeModeProvider = GrimodexFixedScopeModeProvider(value: scopeMode)
        self.clientContext = clientContext
    }

    init(
        snapshotProvider: any GrimodexSnapshotProviding,
        scopeModeProvider: any GrimodexScopeModeProviding,
        clientContext: GrimodexClientContext
    ) {
        self.snapshotProvider = snapshotProvider
        self.scopeModeProvider = scopeModeProvider
        self.clientContext = clientContext
    }

    func latest() -> GrimodexIntegrationRevision {
        GrimodexIntegrationRevision(
            snapshot: snapshotProvider.latest(),
            decision: GrimodexScopePolicy.evaluate(
                mode: scopeModeProvider.current(),
                context: clientContext
            )
        )
    }
}

final class HazkeySessionRegistry {
    typealias RevisionProviderFactory =
        (GrimodexClientContext) -> any GrimodexRevisionProviding

    private struct Session {
        let ownerFd: Int32
        let clientContext: GrimodexClientContext
        let state: HazkeyServerState
        var lastAccess: Date
    }

    let serverConfig: HazkeyServerConfig
    private let revisionProviderFactory: RevisionProviderFactory
    private let maximumSessions: Int
    private let maximumSessionsPerOwner: Int
    private let idleTimeout: TimeInterval
    private let now: () -> Date
    private let idleLearningDataClearer: () -> Void
    private var sessions: [String: Session] = [:]

    var count: Int {
        pruneExpiredSessions()
        return sessions.count
    }

    init(
        serverConfig: HazkeyServerConfig = HazkeyServerConfig(),
        revisionProviderFactory: @escaping RevisionProviderFactory = {
            _ in GrimodexDisabledRevisionProvider()
        },
        maximumSessions: Int = 128,
        maximumSessionsPerOwner: Int = 16,
        idleTimeout: TimeInterval = 30 * 60,
        now: @escaping () -> Date = { Date() },
        idleLearningDataClearer: (() -> Void)? = nil
    ) {
        self.serverConfig = serverConfig
        self.revisionProviderFactory = revisionProviderFactory
        self.maximumSessions = max(1, maximumSessions)
        self.maximumSessionsPerOwner = max(
            1,
            min(maximumSessionsPerOwner, max(1, maximumSessions))
        )
        self.idleTimeout = max(1, idleTimeout)
        self.now = now
        self.idleLearningDataClearer = idleLearningDataClearer ?? {
            let state = HazkeyServerState(serverConfig: serverConfig)
            _ = state.clearProfileLearningData()
        }
    }

    @discardableResult
    func open(
        clientContext: GrimodexClientContext,
        ownerFd: Int32
    ) -> String {
        pruneExpiredSessions()
        while sessions.values.lazy.filter({ $0.ownerFd == ownerFd }).count
            >= maximumSessionsPerOwner
        {
            guard let leastRecentlyUsed = leastRecentlyUsedSession(ownerFd: ownerFd) else {
                break
            }
            removeSession(sessionID: leastRecentlyUsed)
        }
        while sessions.count >= maximumSessions {
            // A single connection must not evict another application's active
            // composition by opening sessions repeatedly. Prefer replacing
            // the requester's own oldest session once the global bound is hit.
            guard let leastRecentlyUsed =
                leastRecentlyUsedSession(ownerFd: ownerFd)
                ?? leastRecentlyUsedSession(ownerFd: nil)
            else { break }
            removeSession(sessionID: leastRecentlyUsed)
        }
        let sessionID = UUID().uuidString.lowercased()
        let state = HazkeyServerState(
            serverConfig: serverConfig,
            revisionProvider: revisionProviderFactory(clientContext)
        )
        state.refreshGrimodexIntegration()
        sessions[sessionID] = Session(
            ownerFd: ownerFd,
            clientContext: clientContext,
            state: state,
            lastAccess: now()
        )
        return sessionID
    }

    func state(for sessionID: String, ownerFd: Int32) -> HazkeyServerState? {
        pruneExpiredSessions()
        guard !sessionID.isEmpty, var session = sessions[sessionID] else {
            return nil
        }
        guard session.ownerFd == ownerFd else { return nil }
        session.lastAccess = now()
        sessions[sessionID] = session
        return session.state
    }

    func clientContext(
        for sessionID: String,
        ownerFd: Int32
    ) -> GrimodexClientContext? {
        pruneExpiredSessions()
        guard var session = sessions[sessionID], session.ownerFd == ownerFd else {
            return nil
        }
        session.lastAccess = now()
        sessions[sessionID] = session
        return session.clientContext
    }

    @discardableResult
    func close(sessionID: String, ownerFd: Int32) -> Bool {
        guard let session = sessions[sessionID], session.ownerFd == ownerFd else {
            return false
        }
        removeSession(sessionID: sessionID)
        return true
    }

    func closeAll(ownerFd: Int32) {
        let ownedSessionIDs = sessions.compactMap { sessionID, session in
            session.ownerFd == ownerFd ? sessionID : nil
        }
        for sessionID in ownedSessionIDs {
            _ = close(sessionID: sessionID, ownerFd: ownerFd)
        }
    }

    func reinitializeAll() {
        for session in sessions.values {
            session.state.reinitializeConfiguration()
        }
    }

    func clearAllLearningData() {
        if sessions.isEmpty {
            idleLearningDataClearer()
            return
        }
        for session in sessions.values {
            _ = session.state.clearProfileLearningData()
        }
    }

    func saveAll() {
        for session in sessions.values {
            _ = session.state.saveLearningData()
        }
    }

    func diagnostics(scopeMode: GrimodexScopeMode) -> GrimodexSessionDiagnostics {
        pruneExpiredSessions()
        let latest = sessions.max { left, right in
            if left.value.lastAccess == right.value.lastAccess {
                return left.key < right.key
            }
            return left.value.lastAccess < right.value.lastAccess
        }?.value
        let decision = latest.map {
            GrimodexScopePolicy.evaluate(mode: scopeMode, context: $0.clientContext)
        }
        return GrimodexSessionDiagnostics(
            activeSessions: sessions.count,
            clientContext: latest?.clientContext,
            scopeDecision: decision
        )
    }

    private func pruneExpiredSessions() {
        let cutoff = now().addingTimeInterval(-idleTimeout)
        let expired = sessions.compactMap { sessionID, session in
            session.lastAccess < cutoff ? sessionID : nil
        }
        for sessionID in expired {
            removeSession(sessionID: sessionID)
        }
    }

    private func removeSession(sessionID: String) {
        guard let session = sessions.removeValue(forKey: sessionID) else { return }
        _ = session.state.saveLearningData()
    }

    private func leastRecentlyUsedSession(ownerFd: Int32?) -> String? {
        sessions
            .filter { ownerFd == nil || $0.value.ownerFd == ownerFd }
            .min { left, right in
                if left.value.lastAccess == right.value.lastAccess {
                    return left.key < right.key
                }
                return left.value.lastAccess < right.value.lastAccess
            }?
            .key
    }
}
