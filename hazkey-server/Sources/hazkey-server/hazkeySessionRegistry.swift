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
    }

    let serverConfig: HazkeyServerConfig
    private let revisionProviderFactory: RevisionProviderFactory
    private var sessions: [String: Session] = [:]

    var count: Int { sessions.count }

    init(
        serverConfig: HazkeyServerConfig = HazkeyServerConfig(),
        revisionProviderFactory: @escaping RevisionProviderFactory = {
            _ in GrimodexDisabledRevisionProvider()
        }
    ) {
        self.serverConfig = serverConfig
        self.revisionProviderFactory = revisionProviderFactory
    }

    @discardableResult
    func open(
        clientContext: GrimodexClientContext,
        ownerFd: Int32
    ) -> String {
        let sessionID = UUID().uuidString.lowercased()
        let state = HazkeyServerState(
            serverConfig: serverConfig,
            revisionProvider: revisionProviderFactory(clientContext)
        )
        state.refreshGrimodexIntegration()
        sessions[sessionID] = Session(
            ownerFd: ownerFd,
            clientContext: clientContext,
            state: state
        )
        return sessionID
    }

    func state(for sessionID: String, ownerFd: Int32) -> HazkeyServerState? {
        guard !sessionID.isEmpty, let session = sessions[sessionID] else {
            return nil
        }
        guard session.ownerFd == ownerFd else { return nil }
        return session.state
    }

    func clientContext(
        for sessionID: String,
        ownerFd: Int32
    ) -> GrimodexClientContext? {
        guard let session = sessions[sessionID], session.ownerFd == ownerFd else {
            return nil
        }
        return session.clientContext
    }

    @discardableResult
    func close(sessionID: String, ownerFd: Int32) -> Bool {
        guard let session = sessions[sessionID], session.ownerFd == ownerFd else {
            return false
        }
        _ = session.state.saveLearningData()
        sessions.removeValue(forKey: sessionID)
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
        for session in sessions.values {
            _ = session.state.clearProfileLearningData()
        }
    }

    func saveAll() {
        for session in sessions.values {
            _ = session.state.saveLearningData()
        }
    }
}
