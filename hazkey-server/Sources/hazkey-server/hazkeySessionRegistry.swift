import Foundation
import KanaKanjiConverterModule

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

private func pinnedKeymap(_ keymap: Keymap) -> [String: PinnedKeymapRule] {
    Dictionary(uniqueKeysWithValues: keymap.map { key, value in
        (
            String(key),
            PinnedKeymapRule(
                intention: String(value.0),
                inputOverride: value.1.map(String.init)
            )
        )
    })
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

enum HazkeySessionOpenError: Error, Equatable {
    case resourceExhausted
}

enum HazkeySessionRemovalReason: Equatable {
    case explicitClose
    case socketDisconnect
    case capacityEviction
    case idleTimeout

    var commitsPendingLearning: Bool {
        switch self {
        case .socketDisconnect:
            // A cancellation decision may be journaled client-side but not yet
            // delivered when the socket disappears. Committing here would
            // invert Backspace/Ctrl-Z and learn text already removed by the app.
            return false
        case .explicitClose, .capacityEviction, .idleTimeout:
            return true
        }
    }
}

final class HazkeySessionRegistry {
    typealias RevisionProviderFactory =
        (GrimodexClientContext) -> any GrimodexRevisionProviding
    typealias DicdataStoreFactory = () -> DicdataStore

    private struct Session {
        let ownerFd: Int32
        let clientContext: GrimodexClientContext
        let environment: HazkeySessionEnvironment
        let semanticController: ImeV2SessionController
        var lastAccess: Date
    }

    let serverConfig: HazkeyServerConfig
    private let revisionProviderFactory: RevisionProviderFactory
    private let dicdataStoreFactory: DicdataStoreFactory
    private let userDictionaryStore: UserDictionaryStore
    private let mozcCore: (any MozcCoreConverting)?
    private let learningRevisionStore = HazkeyLearningRevisionStore()
    private let zenzaiRuntimeDiagnosticsStore = ZenzaiRuntimeDiagnosticsStore()
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
        dicdataStoreFactory: DicdataStoreFactory? = nil,
        userDictionaryStore: UserDictionaryStore? = nil,
        mozcCore: (any MozcCoreConverting)? = nil,
        maximumSessions: Int = 128,
        maximumSessionsPerOwner: Int = 16,
        idleTimeout: TimeInterval = 30 * 60,
        now: @escaping () -> Date = { Date() },
        idleLearningDataClearer: (() -> Void)? = nil
    ) {
        self.serverConfig = serverConfig
        self.revisionProviderFactory = revisionProviderFactory
        self.dicdataStoreFactory = dicdataStoreFactory ?? {
            DicdataStore(dictionaryURL: serverConfig.dictionaryPath)
        }
        self.userDictionaryStore = userDictionaryStore ?? UserDictionaryStore(
            persistenceURL: HazkeyServerConfig.getDataDirectory()
                .appendingPathComponent("user-dictionary-v1.json")
        )
        if serverConfig.converterBackend == .mozc {
            self.mozcCore = mozcCore ?? MozcSidecarClient(
                helperPath: serverConfig.mozcHelperPath,
                dataPath: serverConfig.mozcDataPath
            )
        } else {
            self.mozcCore = nil
        }
        self.maximumSessions = max(1, maximumSessions)
        self.maximumSessionsPerOwner = max(
            1,
            min(maximumSessionsPerOwner, max(1, maximumSessions))
        )
        self.idleTimeout = max(1, idleTimeout)
        self.now = now
        self.idleLearningDataClearer = idleLearningDataClearer ?? {
            let environment = HazkeySessionEnvironment(serverConfig: serverConfig)
            environment.clearProfileLearningData()
        }
        zenzaiRuntimeDiagnosticsStore.reset(
            decision: serverConfig.zenzaiRuntimeDecision(
                zenzaiAllowed: serverConfig.converterBackend != .mozc
            )
        )
    }

    @discardableResult
    func open(
        clientContext: GrimodexClientContext,
        ownerFd: Int32,
        clientFeatureBits: UInt64 = ImeV2ClientFeatures.current
    ) -> String {
        switch attemptOpen(
            clientContext: clientContext,
            ownerFd: ownerFd,
            clientFeatureBits: clientFeatureBits
        ) {
        case .success(let sessionID):
            return sessionID
        case .failure(.resourceExhausted):
            preconditionFailure("Session capacity exhausted")
        }
    }

    @discardableResult
    func attemptOpen(
        clientContext: GrimodexClientContext,
        ownerFd: Int32,
        clientFeatureBits: UInt64 = ImeV2ClientFeatures.current
    ) -> Result<String, HazkeySessionOpenError> {
        pruneExpiredSessions()
        while sessions.values.lazy.filter({ $0.ownerFd == ownerFd }).count
            >= maximumSessionsPerOwner
        {
            guard let leastRecentlyUsed = leastRecentlyUsedSession(ownerFd: ownerFd) else {
                break
            }
            removeSession(sessionID: leastRecentlyUsed, reason: .capacityEviction)
        }
        while sessions.count >= maximumSessions {
            // Preserve every foreign owner's active composition. A requester
            // may replace only one of its own sessions when the global bound
            // is reached; a new owner receives an explicit capacity error.
            guard let leastRecentlyUsed = leastRecentlyUsedSession(ownerFd: ownerFd) else {
                return .failure(.resourceExhausted)
            }
            removeSession(sessionID: leastRecentlyUsed, reason: .capacityEviction)
        }
        let sessionID = UUID().uuidString.lowercased()
        let store = dicdataStoreFactory()
        let converter = KanaKanjiConverter(dicdataStore: store)
        let boundaryConverter = KanaKanjiConverter(dicdataStore: store)
        let environment = HazkeySessionEnvironment(
            serverConfig: serverConfig,
            revisionProvider: revisionProviderFactory(clientContext),
            converter: converter,
            boundaryConverter: boundaryConverter
        )
        environment.refreshGrimodexIntegration()
        environment.replaceUserDictionary(userDictionaryStore.entries)
        let appliedRevision = environment.grimodexAppliedRevision
        let supportsScheduledLiveConversion =
            clientFeatureBits & ImeV2ClientFeatures.scheduleLiveConversionEffect != 0
        let supportsStagedLearningResolution =
            clientFeatureBits & ImeV2ClientFeatures.stagedLearningResolution != 0
        let liveConversionDelayMilliseconds = supportsScheduledLiveConversion
            ? environment.grimodexLiveConversionDelayMilliseconds
            : 0
        let usesMozc = serverConfig.converterBackend == .mozc
        let semanticSession = CompositionSession(
            sessionID: sessionID,
            context: SessionContext(
                sessionID: sessionID,
                leftContext: "",
                projectRevision: appliedRevision?.generation ?? 0
            ),
            policy: PinnedCompositionPolicy(
                allowsLearning: usesMozc
                    ? false
                    : environment.grimodexAllowsLearning,
                secureInput: environment.grimodexSecureInput,
                zenzaiEnabled: usesMozc
                    ? false
                    : !environment.grimodexSecureInput,
                projectRevision: appliedRevision?.generation ?? 0,
                autoConvertMode: environment.grimodexAutoConvertMode,
                liveConversionDelayMilliseconds: liveConversionDelayMilliseconds,
                suggestionListMode: environment.grimodexSuggestionListMode,
                suggestionListLimit: Int(
                    environment.serverConfig.currentProfile.numSuggestions
                ),
                auxTextMode: environment.grimodexAuxTextMode,
                directCommitTargets: environment.grimodexDirectCommitTargets,
                inputTableName: environment.currentTableName,
                keymap: pinnedKeymap(environment.keymap)
            )
        )
        let zenzaiRuntimeDiagnosticsStore = self.zenzaiRuntimeDiagnosticsStore
        let productionConverter: any KanaKanjiConverting
        if usesMozc {
            guard let mozcCore else {
                preconditionFailure("Mozc backend selected without a core supervisor")
            }
            productionConverter = MozcKanaKanjiConverterAdapter(
                core: mozcCore,
                mappedInputStyleProvider: { [environment] in
                    .mapped(id: .tableName(environment.currentTableName))
                },
                projectDictionaryIndexProvider: { [environment] in
                    environment.grimodexProjectDictionaryIndex
                },
                userDictionaryIndexProvider: {
                    [userDictionaryStore = self.userDictionaryStore] in
                    userDictionaryStore.candidateIndexSnapshot
                }
            )
        } else {
            productionConverter = HazkeyKanaKanjiConverterAdapter(
                converter: converter,
                boundaryConverter: environment.boundaryConverter,
                optionsProvider: { [environment] options in
                    var requestOptions = environment.baseConvertRequestOptions
                    requestOptions.zenzaiMode = environment.serverConfig.genZenzaiMode(
                        leftContext: options.leftContext,
                        rightContext: options.rightContext,
                        projectConditions: environment.grimodexActiveConditions,
                        zenzaiAllowed: options.zenzaiEnabled
                    )
                    return requestOptions
                },
                mappedInputStyleProvider: { [environment] in
                    .mapped(id: .tableName(environment.currentTableName))
                },
                projectDictionaryIndexProvider: { [environment] in
                    environment.grimodexProjectDictionaryIndex
                },
                zenzaiDiagnosticsReporter: { [environment] options, status in
                    zenzaiRuntimeDiagnosticsStore.record(
                        decision: environment.serverConfig.zenzaiRuntimeDecision(
                            zenzaiAllowed: options.zenzaiEnabled
                        ),
                        converterStatus: status
                    )
                }
            )
        }
        let reducerConverter: any KanaKanjiConverting = if usesMozc {
            productionConverter
        } else {
            LearningSynchronizedKanaKanjiConverter(
                base: productionConverter,
                revisionStore: learningRevisionStore
            )
        }
        let semanticController = ImeV2SessionController(
            reducer: ImeReducer(
                session: semanticSession,
                converter: reducerConverter,
                stagedLearningEnabled: supportsStagedLearningResolution
            ),
            policyProvider: { [environment] in
                environment.refreshGrimodexIntegration()
                let revision = environment.grimodexAppliedRevision
                return PinnedCompositionPolicy(
                    allowsLearning: usesMozc
                        ? false
                        : environment.grimodexAllowsLearning,
                    secureInput: environment.grimodexSecureInput,
                    zenzaiEnabled: usesMozc
                        ? false
                        : !environment.grimodexSecureInput,
                    projectRevision: revision?.generation ?? 0,
                    autoConvertMode: environment.grimodexAutoConvertMode,
                    liveConversionDelayMilliseconds: supportsScheduledLiveConversion
                        ? environment.grimodexLiveConversionDelayMilliseconds
                        : 0,
                    suggestionListMode: environment.grimodexSuggestionListMode,
                    suggestionListLimit: Int(
                        environment.serverConfig.currentProfile.numSuggestions
                    ),
                    auxTextMode: environment.grimodexAuxTextMode,
                    directCommitTargets: environment.grimodexDirectCommitTargets,
                    inputTableName: environment.currentTableName,
                    keymap: pinnedKeymap(environment.keymap)
                )
            }
        )
        sessions[sessionID] = Session(
            ownerFd: ownerFd,
            clientContext: clientContext,
            environment: environment,
            semanticController: semanticController,
            lastAccess: now()
        )
        return .success(sessionID)
    }

    func environment(
        for sessionID: String,
        ownerFd: Int32
    ) -> HazkeySessionEnvironment? {
        pruneExpiredSessions()
        guard !sessionID.isEmpty, var session = sessions[sessionID] else {
            return nil
        }
        guard session.ownerFd == ownerFd else { return nil }
        session.lastAccess = now()
        sessions[sessionID] = session
        return session.environment
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

    func semanticController(
        for sessionID: String,
        ownerFd: Int32
    ) -> ImeV2SessionController? {
        pruneExpiredSessions()
        guard var session = sessions[sessionID], session.ownerFd == ownerFd else {
            return nil
        }
        session.lastAccess = now()
        sessions[sessionID] = session
        return session.semanticController
    }

    @discardableResult
    func close(sessionID: String, ownerFd: Int32) -> Bool {
        guard let session = sessions[sessionID], session.ownerFd == ownerFd else {
            return false
        }
        removeSession(sessionID: sessionID, reason: .explicitClose)
        return true
    }

    func closeAll(ownerFd: Int32) {
        let ownedSessionIDs = sessions.compactMap { sessionID, session in
            session.ownerFd == ownerFd ? sessionID : nil
        }
        for sessionID in ownedSessionIDs {
            removeSession(sessionID: sessionID, reason: .socketDisconnect)
        }
    }

    func reinitializeAll() {
        zenzaiRuntimeDiagnosticsStore.reset(
            decision: serverConfig.zenzaiRuntimeDecision(
                zenzaiAllowed: serverConfig.converterBackend != .mozc
            )
        )
        for session in sessions.values {
            session.environment.reinitializeConfiguration()
        }
    }

    func clearAllLearningData() {
        if sessions.isEmpty {
            idleLearningDataClearer()
            _ = learningRevisionStore.recordCommit()
            return
        }
        for session in sessions.values {
            // A staged transaction was derived from the history that is about
            // to be deleted. Discard it first so the next client action cannot
            // resurrect cleared learning data.
            session.semanticController.finalizePendingLearning(commit: false)
            session.environment.clearProfileLearningData()
        }
        _ = learningRevisionStore.recordCommit()
    }

    func userDictionaryEntries() -> [UserDictionaryEntry] {
        userDictionaryStore.entries
    }

    @discardableResult
    func addUserDictionaryEntry(
        _ entry: UserDictionaryEntry
    ) throws -> UserDictionaryEntry {
        let result = try userDictionaryStore.add(entry)
        applyUserDictionaryToSessions()
        return result
    }

    func updateUserDictionaryEntry(_ entry: UserDictionaryEntry) throws {
        try userDictionaryStore.update(entry)
        applyUserDictionaryToSessions()
    }

    func removeUserDictionaryEntry(id: String) throws {
        try userDictionaryStore.remove(id: id)
        applyUserDictionaryToSessions()
    }

    func importUserDictionary(_ data: Data, merge: Bool) throws {
        try userDictionaryStore.importJSON(data, merge: merge)
        applyUserDictionaryToSessions()
    }

    func exportUserDictionary() throws -> Data {
        try userDictionaryStore.exportJSON()
    }

    func saveAll() {
        for session in sessions.values {
            session.semanticController.finalizePendingLearning(commit: true)
            session.environment.close()
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

    func zenzaiRuntimeDiagnostics() -> ZenzaiRuntimeDiagnosticsSnapshot {
        pruneExpiredSessions()
        return zenzaiRuntimeDiagnosticsStore.snapshot()
    }

    private func pruneExpiredSessions() {
        let cutoff = now().addingTimeInterval(-idleTimeout)
        let expired = sessions.compactMap { sessionID, session in
            session.lastAccess < cutoff ? sessionID : nil
        }
        for sessionID in expired {
            removeSession(sessionID: sessionID, reason: .idleTimeout)
        }
    }

    private func removeSession(
        sessionID: String,
        reason: HazkeySessionRemovalReason
    ) {
        guard let session = sessions.removeValue(forKey: sessionID) else { return }
        session.semanticController.finalizePendingLearning(
            commit: reason.commitsPendingLearning
        )
        session.environment.close()
    }

    private func applyUserDictionaryToSessions() {
        let entries = userDictionaryStore.entries
        for session in sessions.values {
            session.semanticController.invalidateForDictionaryChange()
            session.environment.replaceUserDictionary(entries)
        }
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
