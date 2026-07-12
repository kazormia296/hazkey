import Foundation

final class GrimodexLinuxRuntime {
    let snapshotManager: GrimodexSnapshotManager

    private let watcher: GrimodexSnapshotWatcher
    private let registrar: GrimodexConsumerRegistrar
    private let scopeModeStore: GrimodexScopeModeStore
    private let lifecycleLock = NSLock()
    private var started = false

    init(
        rootURL: URL = GrimodexPathResolver.resolve(),
        version: String,
        initialScopeMode: GrimodexScopeMode = .defaultValue,
        watcherRetryInterval: TimeInterval = 0.1,
        watcherMaxRearmAttempts: Int = 5,
        watcherBeforeReconcile: @escaping @Sendable () throws -> Void = {},
        consumerHeartbeatInterval: TimeInterval = GrimodexConsumerRegistrar.heartbeatInterval
    ) {
        let manager = GrimodexSnapshotManager(
            loader: GrimodexSnapshotLoader(rootURL: rootURL)
        )
        snapshotManager = manager
        scopeModeStore = GrimodexScopeModeStore(initialScopeMode)
        watcher = GrimodexSnapshotWatcher(
            rootURL: rootURL,
            retryInterval: watcherRetryInterval,
            maxRearmAttempts: watcherMaxRearmAttempts,
            beforeReconcile: watcherBeforeReconcile
        ) {
            manager.reload().diagnostic.isRetryable
        }
        registrar = GrimodexConsumerRegistrar(
            rootURL: rootURL,
            version: version,
            heartbeatInterval: consumerHeartbeatInterval
        )
    }

    func start() {
        lifecycleLock.lock()
        guard !started else {
            lifecycleLock.unlock()
            return
        }
        started = true
        lifecycleLock.unlock()

        _ = snapshotManager.reload()
        do {
            try watcher.start()
        } catch {
            NSLog("Failed to start Grimodex snapshot watcher: \(error)")
            do {
                try registrar.unregister()
            } catch {
                NSLog("Failed to withdraw Grimodex IME consumer: \(error)")
            }
            return
        }
        do {
            try registrar.start()
        } catch {
            NSLog("Failed to register Grimodex IME consumer: \(error)")
        }
    }

    func stop() {
        lifecycleLock.lock()
        guard started else {
            lifecycleLock.unlock()
            return
        }
        started = false
        lifecycleLock.unlock()

        do {
            try registrar.unregister()
        } catch {
            NSLog("Failed to unregister Grimodex IME consumer: \(error)")
        }
        watcher.stop()
    }

    func revisionProvider(
        scopeMode: GrimodexScopeMode,
        clientContext: GrimodexClientContext
    ) -> GrimodexSessionRevisionProvider {
        GrimodexSessionRevisionProvider(
            snapshotProvider: snapshotManager,
            scopeMode: scopeMode,
            clientContext: clientContext
        )
    }

    func revisionProvider(
        clientContext: GrimodexClientContext
    ) -> GrimodexSessionRevisionProvider {
        GrimodexSessionRevisionProvider(
            snapshotProvider: snapshotManager,
            scopeModeProvider: scopeModeStore,
            clientContext: clientContext
        )
    }

    func updateScopeMode(_ scopeMode: GrimodexScopeMode) {
        scopeModeStore.update(scopeMode)
    }

    func diagnostics() -> GrimodexRuntimeDiagnostics {
        return GrimodexRuntimeDiagnostics(
            watcherActive: watcher.isActive,
            consumerRegistered: registrar.isRegistered,
            snapshot: snapshotManager.latest()
        )
    }
}
