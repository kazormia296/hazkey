import Foundation

if ABProbeCommand.isRequested {
    do {
        try ABProbeCommand.run()
        exit(0)
    } catch {
        FileHandle.standardError.write(Data("AB probe failed: \(error.localizedDescription)\n".utf8))
        exit(2)
    }
}

do {
    NSLog("Starting \(GrimodexProductPaths.serverExecutableName)...")
    let server = HazkeyServer()

    try server.start()
} catch {
    NSLog("Failed to start server: \(error)")
    exit(1)
}
