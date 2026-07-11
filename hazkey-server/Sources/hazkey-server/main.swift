import Foundation

do {
    NSLog("Starting \(GrimodexProductPaths.serverExecutableName)...")
    let server = HazkeyServer()

    try server.start()
} catch {
    NSLog("Failed to start server: \(error)")
    exit(1)
}
