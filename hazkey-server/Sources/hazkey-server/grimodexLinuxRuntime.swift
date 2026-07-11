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
        initialScopeMode: GrimodexScopeMode = .defaultValue
    ) {
        let manager = GrimodexSnapshotManager(
            loader: GrimodexSnapshotLoader(rootURL: rootURL)
        )
        snapshotManager = manager
        scopeModeStore = GrimodexScopeModeStore(initialScopeMode)
        watcher = GrimodexSnapshotWatcher(rootURL: rootURL) {
            manager.reload().diagnostic.isRetryable
        }
        registrar = GrimodexConsumerRegistrar(
            rootURL: rootURL,
            version: version
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

        registrar.stop()
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
}
